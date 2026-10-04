"""Durable request/result queue shared by Render and the Windows MT5 worker."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.metaapi_gateway import MetaApiGatewayError
from app.models import LocalBridgeCommand, LocalBridgeWorker

TERMINAL_STATUSES = {"succeeded", "failed", "ambiguous"}


class LocalBridgeQueue:
    """Enqueue broker work and wait for a worker-confirmed result.

    Mutations are never automatically reissued with a new command id. A claimed command
    may only be reclaimed by the same worker id, which must consult its local journal
    before touching MT5 again. This makes an interrupted mutation ambiguous instead of
    silently duplicating an order.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        profile: str = "super-signals",
        timeout_seconds: float = 30.0,
        poll_seconds: float = 0.20,
    ) -> None:
        self._session_factory = session_factory
        self.profile = profile.strip()
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.poll_seconds = min(max(float(poll_seconds), 0.05), 2.0)
        if not self.profile:
            raise ValueError("local_bridge_profile_required")

    async def execute(
        self,
        *,
        account_id: str,
        operation: str,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        command_id = await asyncio.to_thread(
            self._enqueue,
            account_id=account_id,
            operation=operation,
            payload=payload or {},
            idempotency_key=idempotency_key,
        )
        deadline = asyncio.get_running_loop().time() + self.timeout_seconds
        while True:
            state = await asyncio.to_thread(self._result_state, command_id)
            if state[0] == "succeeded":
                return state[1]
            if state[0] == "failed":
                raise MetaApiGatewayError(
                    str(state[2] or "local_bridge_command_failed"),
                    retryable=bool(state[3]),
                )
            if state[0] == "ambiguous":
                raise MetaApiGatewayError("metaapi_timeout", retryable=True)
            if asyncio.get_running_loop().time() >= deadline:
                await asyncio.to_thread(self._mark_timeout, command_id)
                # Preserve the canonical ambiguity/compensation path during migration.
                raise MetaApiGatewayError("metaapi_timeout", retryable=True)
            await asyncio.sleep(self.poll_seconds)

    def _enqueue(
        self,
        *,
        account_id: str,
        operation: str,
        payload: dict[str, Any],
        idempotency_key: str | None,
    ) -> UUID:
        normalized_account = account_id.strip()
        normalized_operation = operation.strip()
        if not normalized_account or not normalized_operation:
            raise MetaApiGatewayError("local_bridge_request_invalid")
        command = LocalBridgeCommand(
            profile=self.profile,
            account_id=normalized_account,
            operation=normalized_operation,
            payload=payload,
            idempotency_key=idempotency_key,
        )
        try:
            with self._session_factory() as session:
                session.add(command)
                session.commit()
                return command.id
        except IntegrityError:
            if not idempotency_key:
                raise
            with self._session_factory() as session:
                existing = session.scalar(
                    select(LocalBridgeCommand).where(
                        LocalBridgeCommand.idempotency_key == idempotency_key
                    )
                )
                if existing is None:
                    raise
                return existing.id

    def _result_state(self, command_id: UUID) -> tuple[str, Any, str | None, bool]:
        with self._session_factory() as session:
            command = session.get(LocalBridgeCommand, command_id)
            if command is None:
                return "failed", None, "local_bridge_command_missing", False
            retryable = command.error_code in {
                "local_bridge_worker_unavailable",
                "local_bridge_mt5_unavailable",
                "local_bridge_timeout",
            }
            return command.status, command.result, command.error_code, retryable

    def _mark_timeout(self, command_id: UUID) -> None:
        with self._session_factory() as session:
            command = session.get(LocalBridgeCommand, command_id, with_for_update=True)
            if command is None or command.status in TERMINAL_STATUSES:
                return
            # A command already handed to MT5 may have executed. Never label it failed
            # and never make it eligible for a different worker to execute again.
            if command.status == "claimed":
                command.status = "ambiguous"
                command.error_code = "local_bridge_timeout"
                command.completed_at = datetime.now(UTC)
                session.commit()
            elif command.status == "queued":
                # No worker received it, so it is safe to fail without ambiguity.
                command.status = "failed"
                command.error_code = "local_bridge_worker_unavailable"
                command.completed_at = datetime.now(UTC)
                session.commit()

    def heartbeat(
        self,
        *,
        worker_id: str,
        profile: str,
        version: str,
        capabilities: list[str],
    ) -> None:
        now = datetime.now(UTC)
        with self._session_factory() as session:
            worker = session.scalar(
                select(LocalBridgeWorker).where(LocalBridgeWorker.worker_id == worker_id)
            )
            if worker is None:
                worker = LocalBridgeWorker(
                    worker_id=worker_id,
                    profile=profile,
                    version=version,
                    capabilities=capabilities,
                    last_seen_at=now,
                )
                session.add(worker)
            else:
                worker.profile = profile
                worker.version = version
                worker.capabilities = capabilities
                worker.last_seen_at = now
            session.commit()

    def claim(
        self,
        *,
        worker_id: str,
        profile: str,
        lease_seconds: int = 45,
    ) -> LocalBridgeCommand | None:
        now = datetime.now(UTC)
        lease_until = now + timedelta(seconds=min(max(lease_seconds, 15), 120))
        with self._session_factory() as session:
            command = session.scalar(
                select(LocalBridgeCommand)
                .where(
                    LocalBridgeCommand.profile == profile,
                    or_(
                        LocalBridgeCommand.status == "queued",
                        (
                            (LocalBridgeCommand.status == "claimed")
                            & (LocalBridgeCommand.claimed_by == worker_id)
                            & (LocalBridgeCommand.lease_expires_at < now)
                        ),
                    ),
                )
                .order_by(LocalBridgeCommand.created_at, LocalBridgeCommand.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if command is None:
                return None
            command.status = "claimed"
            command.claimed_by = worker_id
            command.lease_token = uuid4()
            command.lease_expires_at = lease_until
            command.attempt_count += 1
            session.commit()
            session.refresh(command)
            session.expunge(command)
            return command

    def complete(
        self,
        *,
        command_id: UUID,
        worker_id: str,
        lease_token: UUID,
        status: str,
        result: Any = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> str:
        if status not in TERMINAL_STATUSES:
            raise ValueError("local_bridge_completion_status_invalid")
        with self._session_factory() as session:
            command = session.get(LocalBridgeCommand, command_id, with_for_update=True)
            if command is None:
                raise LookupError("local_bridge_command_missing")
            if command.status in TERMINAL_STATUSES:
                return command.status
            if command.claimed_by != worker_id or command.lease_token != lease_token:
                raise PermissionError("local_bridge_lease_invalid")
            command.status = status
            command.result = result
            command.error_code = error_code
            command.error_message = (error_message or "")[:1000] or None
            command.completed_at = datetime.now(UTC)
            session.commit()
            return status


__all__ = ["LocalBridgeQueue", "TERMINAL_STATUSES"]
