"""Pending-order reconciliation shared by paper and future LIVE execution.

The broker is authoritative for pending-order fills and terminal order outcomes. The
reconciliation algorithm is identical for DEMO and LIVE; only account eligibility
differs. LIVE accounts are read only when the global live-execution switch is enabled,
and must match an active MT5 approval before any broker state is trusted.
"""

from __future__ import annotations

import asyncio
import logging
import os
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.metaapi_read_gateway import MetaApiReadGateway
from app.mt5_crypto import MetaApiTokenCipher
from app.pending_reconciliation_canonical import (
    AccountPendingReconciler,
    PendingReconcileResult,
)

logger = logging.getLogger(__name__)


def _live_enabled() -> bool:
    return os.getenv("SUPER_SIGNALS_LIVE_EXECUTION_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


class LiveMemberPendingReconciler(AccountPendingReconciler):
    """Use the exact shared pending reconciliation algorithm for an approved LIVE account."""

    account_environment = "live"

    def _broker_account(self) -> tuple[str, bytes] | None:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT
                        m.metaapi_account_id,
                        m.metaapi_token_ciphertext,
                        m.account_environment,
                        m.status,
                        m.login,
                        m.server,
                        a.login AS approved_login,
                        a.server AS approved_server
                    FROM users AS u
                    JOIN user_roles AS ur ON ur.user_id=u.id
                    JOIN roles AS r ON r.id=ur.role_id AND r.name='user'
                    JOIN mt5_accounts AS m ON m.owner_user_id=u.id
                    JOIN mt5_account_approvals AS a
                      ON a.user_id=u.id
                     AND a.status='active'
                    WHERE u.id=:user_id
                      AND u.status='active'
                      AND m.status!='revoked'
                    ORDER BY m.created_at DESC
                    LIMIT 1
                    """
                ),
                {"user_id": self._owner_user_id},
            ).mappings().first()
        if row is None:
            return None
        if str(row["account_environment"] or "").strip().lower() != "live":
            return None
        if str(row["status"] or "") != "connected":
            return None
        if str(row["login"] or "") != str(row["approved_login"] or ""):
            return None
        if str(row["server"] or "").strip().lower() != str(
            row["approved_server"] or ""
        ).strip().lower():
            return None
        return str(row["metaapi_account_id"]), bytes(row["metaapi_token_ciphertext"])


class UnifiedPendingReconciler:
    """Continuously reconcile Owner DEMO plus enabled LIVE member pending truth."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        cipher: MetaApiTokenCipher,
        gateway: MetaApiReadGateway,
        owner_user_id: UUID,
        poll_seconds: int = 3,
    ) -> None:
        if poll_seconds < 1:
            raise ValueError("pending_poll_seconds_invalid")
        self._session_factory = session_factory
        self._cipher = cipher
        self._gateway = gateway
        self._owner_user_id = owner_user_id
        self._poll_seconds = poll_seconds
        self._owner = AccountPendingReconciler(
            session_factory=session_factory,
            cipher=cipher,
            gateway=gateway,
            owner_user_id=owner_user_id,
            poll_seconds=poll_seconds,
        )
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(
            self._run(),
            name="super-signals-unified-pending-reconciler",
        )

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unified pending reconciliation failed safely")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                pass

    async def reconcile_once(self) -> PendingReconcileResult:
        results = [await self._owner.reconcile_once()]
        if _live_enabled():
            for user_id in self._live_pending_users():
                reconciler = LiveMemberPendingReconciler(
                    session_factory=self._session_factory,
                    cipher=self._cipher,
                    gateway=self._gateway,
                    owner_user_id=user_id,
                    poll_seconds=self._poll_seconds,
                )
                results.append(await reconciler.reconcile_once())
        return PendingReconcileResult(
            pending_seen=sum(item.pending_seen for item in results),
            fills_mapped=sum(item.fills_mapped for item in results),
            still_pending=sum(item.still_pending for item in results),
            unresolved=sum(item.unresolved for item in results),
            terminalized=sum(item.terminalized for item in results),
        )

    def _live_pending_users(self) -> tuple[UUID, ...]:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT DISTINCT p.user_id
                    FROM positions AS p
                    JOIN users AS u ON u.id=p.user_id AND u.status='active'
                    JOIN user_roles AS ur ON ur.user_id=u.id
                    JOIN roles AS r ON r.id=ur.role_id AND r.name='user'
                    WHERE p.status='pending'
                      AND p.user_id != :owner_user_id
                    ORDER BY p.user_id
                    """
                ),
                {"owner_user_id": self._owner_user_id},
            ).scalars().all()
        return tuple(UUID(str(value)) for value in rows)


__all__ = ["LiveMemberPendingReconciler", "UnifiedPendingReconciler"]
