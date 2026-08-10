"""Secure Day 22 connection lifecycle for the owner's Vantage MT5 demo account."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.metaapi_gateway import (
    MetaApiAccountState,
    MetaApiGatewayError,
    MetaApiProvisioningGateway,
)
from app.models import AuditEvent
from app.mt5_crypto import BrokerCredentialDecryptionError, MetaApiTokenCipher


class Mt5ConnectionError(RuntimeError):
    """Safe application-level MT5 connection error."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class Mt5ConnectionView:
    configured: bool
    account_id: UUID | None
    broker: str
    platform: str
    account_environment: str
    login_masked: str | None
    server: str | None
    status: str
    remote_state: str | None
    remote_connection_status: str | None
    last_error_code: str | None
    last_checked_at: datetime | None
    last_connected_at: datetime | None


class Mt5DemoConnectionService:
    """Provision and monitor one owner demo account without storing broker passwords."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        cipher: MetaApiTokenCipher,
        gateway: MetaApiProvisioningGateway,
    ) -> None:
        self._session_factory = session_factory
        self._cipher = cipher
        self._gateway = gateway

    async def connect_owner_demo(
        self,
        *,
        owner_user_id: UUID,
        metaapi_token: str,
        login: str,
        password: str,
        server: str,
    ) -> Mt5ConnectionView:
        token = metaapi_token.strip()
        normalized_login = login.strip()
        normalized_server = server.strip()
        self._validate_input(token, normalized_login, password, normalized_server)

        existing = self._load_row(owner_user_id)
        if existing is not None and (
            str(existing["login"]) != normalized_login
            or str(existing["server"]).casefold() != normalized_server.casefold()
        ):
            raise Mt5ConnectionError("mt5_account_already_bound")

        try:
            remote = await self._gateway.find_account(
                token=token,
                login=normalized_login,
                server=normalized_server,
            )
            if remote is None:
                remote = await self._provision_new_account(
                    token=token,
                    login=normalized_login,
                    password=password,
                    server=normalized_server,
                )
        except MetaApiGatewayError as exc:
            self._audit_failed_attempt(owner_user_id, exc.code)
            raise Mt5ConnectionError(exc.code) from exc

        local_account_id = self._store_connection(
            owner_user_id=owner_user_id,
            token=token,
            login=normalized_login,
            server=normalized_server,
            remote=remote,
        )

        try:
            remote = await self._ensure_deployed_and_poll_connected(
                token=token,
                remote=remote,
                max_wait_seconds=75,
            )
            self._update_remote_state(local_account_id, remote, error_code=None)
        except MetaApiGatewayError as exc:
            self._mark_error(local_account_id, exc.code)

        return self.get_status(owner_user_id)

    async def refresh_owner_demo(self, owner_user_id: UUID) -> Mt5ConnectionView:
        row = self._load_row(owner_user_id)
        if row is None:
            return self._empty_view()
        await self._refresh_row(row)
        return self.get_status(owner_user_id)

    async def reconcile_all(self) -> int:
        """Re-read remote connection state after startup and during normal operation."""

        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT *
                    FROM mt5_accounts
                    WHERE status != 'revoked'
                    ORDER BY created_at ASC
                    """
                )
            ).mappings().all()
        checked = 0
        for row in rows:
            try:
                await self._refresh_row(row)
            except Exception:
                # One broken account must never terminate the connection monitor.
                # _refresh_row records sanitized failures whenever possible.
                pass
            checked += 1
        return checked

    def get_status(self, owner_user_id: UUID) -> Mt5ConnectionView:
        row = self._load_row(owner_user_id)
        if row is None:
            return self._empty_view()
        return self._view_from_row(row)

    async def _refresh_row(self, row: Any) -> None:
        local_account_id = row["id"]
        try:
            token = self._cipher.decrypt(bytes(row["metaapi_token_ciphertext"]))
        except BrokerCredentialDecryptionError:
            self._mark_error(local_account_id, "broker_credential_decryption_failed")
            return

        try:
            remote = await self._gateway.read_account(
                token=token,
                account_id=str(row["metaapi_account_id"]),
            )
            if remote.state in {"CREATED", "UNDEPLOYED", "DEPLOY_FAILED", "UNDEPLOY_FAILED"}:
                await self._gateway.deploy_account(
                    token=token,
                    account_id=remote.account_id,
                )
                remote = await self._gateway.read_account(
                    token=token,
                    account_id=remote.account_id,
                )
            self._update_remote_state(local_account_id, remote, error_code=None)
        except MetaApiGatewayError as exc:
            if exc.code in {"metaapi_timeout", "metaapi_unreachable", "metaapi_temporarily_unavailable"}:
                self._mark_disconnected(local_account_id, exc.code)
            else:
                self._mark_error(local_account_id, exc.code)

    async def _provision_new_account(
        self,
        *,
        token: str,
        login: str,
        password: str,
        server: str,
    ) -> MetaApiAccountState:
        transaction_id = self._gateway.new_transaction_id()
        deadline = time.monotonic() + 150
        while True:
            result = await self._gateway.create_account(
                token=token,
                login=login,
                password=password,
                server=server,
                transaction_id=transaction_id,
            )
            if not result.pending:
                if result.account_id is None:
                    raise MetaApiGatewayError("metaapi_invalid_response")
                return await self._gateway.read_account(
                    token=token,
                    account_id=result.account_id,
                )
            if time.monotonic() + result.retry_after_seconds >= deadline:
                raise MetaApiGatewayError("metaapi_provisioning_timeout", retryable=True)
            await asyncio.sleep(result.retry_after_seconds)

    async def _ensure_deployed_and_poll_connected(
        self,
        *,
        token: str,
        remote: MetaApiAccountState,
        max_wait_seconds: int,
    ) -> MetaApiAccountState:
        if remote.state != "DEPLOYED":
            await self._gateway.deploy_account(token=token, account_id=remote.account_id)

        deadline = time.monotonic() + max_wait_seconds
        latest = remote
        while time.monotonic() < deadline:
            latest = await self._gateway.read_account(
                token=token,
                account_id=remote.account_id,
            )
            if latest.state in {"DEPLOY_FAILED", "REDEPLOY_FAILED"}:
                raise MetaApiGatewayError("metaapi_deploy_failed")
            if latest.state == "DEPLOYED" and latest.connection_status == "CONNECTED":
                return latest
            await asyncio.sleep(2)
        return latest

    def _store_connection(
        self,
        *,
        owner_user_id: UUID,
        token: str,
        login: str,
        server: str,
        remote: MetaApiAccountState,
    ) -> UUID:
        ciphertext = self._cipher.encrypt(token)
        fingerprint = self._cipher.fingerprint(token)
        status = self._local_status(remote)
        now = datetime.now(UTC)
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    INSERT INTO mt5_accounts (
                        owner_user_id,
                        broker,
                        platform,
                        account_environment,
                        login,
                        server,
                        metaapi_account_id,
                        metaapi_token_ciphertext,
                        metaapi_token_fingerprint,
                        status,
                        remote_state,
                        remote_connection_status,
                        last_error_code,
                        last_checked_at,
                        last_connected_at,
                        updated_at
                    ) VALUES (
                        :owner_user_id,
                        'vantage',
                        'mt5',
                        'demo',
                        :login,
                        :server,
                        :metaapi_account_id,
                        :ciphertext,
                        :fingerprint,
                        :status,
                        :remote_state,
                        :remote_connection_status,
                        NULL,
                        :checked_at,
                        :connected_at,
                        :updated_at
                    )
                    ON CONFLICT (owner_user_id)
                    DO UPDATE SET
                        metaapi_account_id = EXCLUDED.metaapi_account_id,
                        metaapi_token_ciphertext = EXCLUDED.metaapi_token_ciphertext,
                        metaapi_token_fingerprint = EXCLUDED.metaapi_token_fingerprint,
                        status = EXCLUDED.status,
                        remote_state = EXCLUDED.remote_state,
                        remote_connection_status = EXCLUDED.remote_connection_status,
                        last_error_code = NULL,
                        last_checked_at = EXCLUDED.last_checked_at,
                        last_connected_at = COALESCE(EXCLUDED.last_connected_at, mt5_accounts.last_connected_at),
                        updated_at = EXCLUDED.updated_at
                    RETURNING id
                    """
                ),
                {
                    "owner_user_id": owner_user_id,
                    "login": login,
                    "server": server,
                    "metaapi_account_id": remote.account_id,
                    "ciphertext": ciphertext,
                    "fingerprint": fingerprint,
                    "status": status,
                    "remote_state": remote.state,
                    "remote_connection_status": remote.connection_status,
                    "checked_at": now,
                    "connected_at": now if status == "connected" else None,
                    "updated_at": now,
                },
            ).scalar_one()
            session.add(
                AuditEvent(
                    actor_user_id=owner_user_id,
                    event_type="mt5.demo_connection_registered",
                    entity_type="mt5_account",
                    entity_id=row,
                    payload={
                        "broker": "vantage",
                        "platform": "mt5",
                        "account_environment": "demo",
                        "login_last4": login[-4:],
                        "server": server,
                        "metaapi_account_id": remote.account_id,
                        "status": status,
                        "password_stored": False,
                        "token_encrypted": True,
                        "trade_action_created": False,
                    },
                )
            )
            session.commit()
            return row

    def _update_remote_state(
        self,
        local_account_id: UUID,
        remote: MetaApiAccountState,
        *,
        error_code: str | None,
    ) -> None:
        status = self._local_status(remote)
        now = datetime.now(UTC)
        with self._session_factory() as session:
            previous = session.execute(
                text("SELECT status FROM mt5_accounts WHERE id = :id FOR UPDATE"),
                {"id": local_account_id},
            ).scalar_one_or_none()
            session.execute(
                text(
                    """
                    UPDATE mt5_accounts
                    SET status = :status,
                        remote_state = :remote_state,
                        remote_connection_status = :remote_connection_status,
                        last_error_code = :last_error_code,
                        last_checked_at = :checked_at,
                        last_connected_at = CASE
                            WHEN :status = 'connected' THEN :checked_at
                            ELSE last_connected_at
                        END,
                        updated_at = :checked_at
                    WHERE id = :id
                    """
                ),
                {
                    "id": local_account_id,
                    "status": status,
                    "remote_state": remote.state,
                    "remote_connection_status": remote.connection_status,
                    "last_error_code": error_code,
                    "checked_at": now,
                },
            )
            if previous != status:
                session.add(
                    AuditEvent(
                        actor_user_id=None,
                        event_type="mt5.connection_status_changed",
                        entity_type="mt5_account",
                        entity_id=local_account_id,
                        payload={
                            "from": previous,
                            "to": status,
                            "remote_state": remote.state,
                            "remote_connection_status": remote.connection_status,
                            "trade_action_created": False,
                        },
                    )
                )
            session.commit()

    def _mark_disconnected(self, local_account_id: UUID, error_code: str) -> None:
        self._set_failure_status(local_account_id, "disconnected", error_code)

    def _mark_error(self, local_account_id: UUID, error_code: str) -> None:
        self._set_failure_status(local_account_id, "error", error_code)

    def _set_failure_status(self, local_account_id: UUID, status: str, error_code: str) -> None:
        now = datetime.now(UTC)
        with self._session_factory() as session:
            previous = session.execute(
                text("SELECT status FROM mt5_accounts WHERE id = :id FOR UPDATE"),
                {"id": local_account_id},
            ).scalar_one_or_none()
            session.execute(
                text(
                    """
                    UPDATE mt5_accounts
                    SET status = :status,
                        last_error_code = :error_code,
                        last_checked_at = :checked_at,
                        updated_at = :checked_at
                    WHERE id = :id
                    """
                ),
                {
                    "id": local_account_id,
                    "status": status,
                    "error_code": error_code,
                    "checked_at": now,
                },
            )
            if previous != status:
                session.add(
                    AuditEvent(
                        actor_user_id=None,
                        event_type="mt5.connection_status_changed",
                        entity_type="mt5_account",
                        entity_id=local_account_id,
                        payload={
                            "from": previous,
                            "to": status,
                            "error_code": error_code,
                            "trade_action_created": False,
                        },
                    )
                )
            session.commit()

    def _audit_failed_attempt(self, owner_user_id: UUID, error_code: str) -> None:
        with self._session_factory() as session:
            session.add(
                AuditEvent(
                    actor_user_id=owner_user_id,
                    event_type="mt5.demo_connection_failed",
                    entity_type="mt5_account",
                    payload={
                        "error_code": error_code,
                        "password_stored": False,
                        "trade_action_created": False,
                    },
                )
            )
            session.commit()

    def _load_row(self, owner_user_id: UUID) -> Any | None:
        with self._session_factory() as session:
            return session.execute(
                text(
                    """
                    SELECT *
                    FROM mt5_accounts
                    WHERE owner_user_id = :owner_user_id
                    LIMIT 1
                    """
                ),
                {"owner_user_id": owner_user_id},
            ).mappings().first()

    @staticmethod
    def _validate_input(token: str, login: str, password: str, server: str) -> None:
        if len(token) < 20:
            raise Mt5ConnectionError("metaapi_token_required")
        if not login.isdigit() or len(login) > 32:
            raise Mt5ConnectionError("mt5_login_invalid")
        if not password or len(password) > 256:
            raise Mt5ConnectionError("mt5_password_invalid")
        if len(server) < 2 or len(server) > 160:
            raise Mt5ConnectionError("mt5_server_invalid")

    @staticmethod
    def _local_status(remote: MetaApiAccountState) -> str:
        if remote.state in {"DEPLOY_FAILED", "REDEPLOY_FAILED", "UNDEPLOY_FAILED"}:
            return "error"
        if remote.state == "DEPLOYED" and remote.connection_status == "CONNECTED":
            return "connected"
        if remote.state in {"CREATED", "DEPLOYING", "UNDEPLOYED"}:
            return "connecting"
        return "disconnected"

    @staticmethod
    def _mask_login(login: str) -> str:
        if len(login) <= 4:
            return "*" * len(login)
        return f"{'*' * max(4, len(login) - 4)}{login[-4:]}"

    def _view_from_row(self, row: Any) -> Mt5ConnectionView:
        return Mt5ConnectionView(
            configured=True,
            account_id=row["id"],
            broker=str(row["broker"]),
            platform=str(row["platform"]),
            account_environment=str(row["account_environment"]),
            login_masked=self._mask_login(str(row["login"])),
            server=str(row["server"]),
            status=str(row["status"]),
            remote_state=(str(row["remote_state"]) if row["remote_state"] else None),
            remote_connection_status=(
                str(row["remote_connection_status"])
                if row["remote_connection_status"]
                else None
            ),
            last_error_code=(str(row["last_error_code"]) if row["last_error_code"] else None),
            last_checked_at=row["last_checked_at"],
            last_connected_at=row["last_connected_at"],
        )

    @staticmethod
    def _empty_view() -> Mt5ConnectionView:
        return Mt5ConnectionView(
            configured=False,
            account_id=None,
            broker="vantage",
            platform="mt5",
            account_environment="demo",
            login_masked=None,
            server=None,
            status="not_configured",
            remote_state=None,
            remote_connection_status=None,
            last_error_code=None,
            last_checked_at=None,
            last_connected_at=None,
        )
