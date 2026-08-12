"""Day 30 approved live Vantage MT5 linking for ordinary users."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.metaapi_gateway import MetaApiGatewayError
from app.models import AuditEvent
from app.mt5_connection_service import Mt5ConnectionError, Mt5ConnectionView
from app.mt5_connection_service_day22 import Day22Mt5DemoConnectionService


@dataclass(frozen=True, slots=True)
class Mt5AccountApprovalView:
    approved: bool
    approval_id: UUID | None
    user_id: UUID
    login_masked: str | None
    server: str | None
    status: str
    approved_at: datetime | None
    updated_at: datetime | None


class Day30Mt5ConnectionService(Day22Mt5DemoConnectionService):
    """Reuse Day 22 encrypted MetaAPI plumbing for one approved live account/user."""

    _TRANSIENT_CODES = {
        "metaapi_timeout",
        "metaapi_unreachable",
        "metaapi_temporarily_unavailable",
        "metaapi_provisioning_timeout",
    }

    @staticmethod
    def validate_live_vantage_account(login: str, server: str) -> tuple[str, str]:
        normalized_login = login.strip()
        normalized_server = server.strip()
        if not normalized_login.isdigit() or len(normalized_login) > 32:
            raise Mt5ConnectionError("mt5_login_invalid")
        if len(normalized_server) < 2 or len(normalized_server) > 160:
            raise Mt5ConnectionError("mt5_server_invalid")
        server_folded = normalized_server.casefold()
        if "vantage" not in server_folded:
            raise Mt5ConnectionError("mt5_vantage_server_required")
        if "demo" in server_folded:
            raise Mt5ConnectionError("mt5_demo_not_available")
        return normalized_login, normalized_server

    def approve_user_account(
        self,
        *,
        approver_user_id: UUID,
        user_id: UUID,
        login: str,
        server: str,
    ) -> Mt5AccountApprovalView:
        normalized_login, normalized_server = self.validate_live_vantage_account(login, server)
        now = datetime.now(UTC)
        with self._session_factory() as session:
            role = session.execute(
                text(
                    """
                    SELECT r.name
                    FROM users AS u
                    JOIN user_roles AS ur ON ur.user_id = u.id
                    JOIN roles AS r ON r.id = ur.role_id
                    WHERE u.id = :user_id
                      AND u.status = 'active'
                    ORDER BY CASE r.name WHEN 'user' THEN 0 ELSE 1 END
                    LIMIT 1
                    """
                ),
                {"user_id": user_id},
            ).scalar_one_or_none()
            if role != "user":
                raise Mt5ConnectionError("mt5_user_not_eligible")

            previous = session.execute(
                text(
                    """
                    SELECT id, login, server, status
                    FROM mt5_account_approvals
                    WHERE user_id = :user_id
                    FOR UPDATE
                    """
                ),
                {"user_id": user_id},
            ).mappings().first()

            approval_id = session.execute(
                text(
                    """
                    INSERT INTO mt5_account_approvals (
                        user_id, broker, platform, account_environment,
                        login, server, approved_by_user_id, status,
                        approved_at, updated_at
                    ) VALUES (
                        :user_id, 'vantage', 'mt5', 'live',
                        :login, :server, :approved_by_user_id, 'active',
                        :approved_at, :updated_at
                    )
                    ON CONFLICT (user_id)
                    DO UPDATE SET
                        login = EXCLUDED.login,
                        server = EXCLUDED.server,
                        approved_by_user_id = EXCLUDED.approved_by_user_id,
                        status = 'active',
                        approved_at = EXCLUDED.approved_at,
                        updated_at = EXCLUDED.updated_at
                    RETURNING id
                    """
                ),
                {
                    "user_id": user_id,
                    "login": normalized_login,
                    "server": normalized_server,
                    "approved_by_user_id": approver_user_id,
                    "approved_at": now,
                    "updated_at": now,
                },
            ).scalar_one()

            changed = previous is not None and (
                str(previous["login"]) != normalized_login
                or str(previous["server"]).casefold() != normalized_server.casefold()
            )
            if changed:
                session.execute(
                    text(
                        """
                        UPDATE mt5_accounts
                        SET status = 'revoked',
                            last_error_code = 'mt5_owner_reapproval_changed',
                            last_checked_at = :now,
                            updated_at = :now
                        WHERE owner_user_id = :user_id
                        """
                    ),
                    {"user_id": user_id, "now": now},
                )

            session.add(
                AuditEvent(
                    actor_user_id=approver_user_id,
                    event_type="mt5.user_account_approved",
                    entity_type="mt5_account_approval",
                    entity_id=approval_id,
                    payload={
                        "user_id": str(user_id),
                        "broker": "vantage",
                        "platform": "mt5",
                        "account_environment": "live",
                        "login_last4": normalized_login[-4:],
                        "server": normalized_server,
                        "approval_changed": changed,
                        "password_requested": False,
                        "metaapi_requested": False,
                        "trade_action_created": False,
                    },
                )
            )
            session.commit()
        return self.get_user_approval(user_id)

    def get_user_approval(self, user_id: UUID) -> Mt5AccountApprovalView:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT id, user_id, login, server, status, approved_at, updated_at
                    FROM mt5_account_approvals
                    WHERE user_id = :user_id
                    LIMIT 1
                    """
                ),
                {"user_id": user_id},
            ).mappings().first()
        if row is None:
            return Mt5AccountApprovalView(
                approved=False,
                approval_id=None,
                user_id=user_id,
                login_masked=None,
                server=None,
                status="not_approved",
                approved_at=None,
                updated_at=None,
            )
        return Mt5AccountApprovalView(
            approved=str(row["status"]) == "active",
            approval_id=row["id"],
            user_id=row["user_id"],
            login_masked=self._mask_login(str(row["login"])),
            server=str(row["server"]),
            status=str(row["status"]),
            approved_at=row["approved_at"],
            updated_at=row["updated_at"],
        )

    async def connect_user_live(
        self,
        *,
        user_id: UUID,
        login: str,
        password: str,
        server: str,
    ) -> Mt5ConnectionView:
        normalized_login, normalized_server = self.validate_live_vantage_account(login, server)
        if not password or len(password) > 256:
            raise Mt5ConnectionError("mt5_password_invalid")

        approval = self._load_active_approval(user_id)
        if approval is None:
            self._audit_user_failure(user_id, "mt5_account_not_approved")
            raise Mt5ConnectionError("mt5_account_not_approved")
        if (
            str(approval["login"]) != normalized_login
            or str(approval["server"]).casefold() != normalized_server.casefold()
        ):
            self._audit_user_failure(user_id, "mt5_account_not_approved")
            raise Mt5ConnectionError("mt5_account_not_approved")

        token = self.resolve_platform_token()
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
            self._audit_user_failure(user_id, exc.code, retryable=exc.retryable)
            raise Mt5ConnectionError(exc.code) from exc

        local_account_id = self._store_user_connection(
            user_id=user_id,
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
            # Reuse the auditable Day 22 writer. Besides preserving the established
            # restart behavior, it avoids the older base helper's psycopg parameter
            # type ambiguity when persisting a newly connected user account.
            self._write_remote_state(local_account_id, remote)
        except MetaApiGatewayError as exc:
            if exc.code in self._TRANSIENT_CODES:
                self._mark_disconnected(local_account_id, exc.code)
            else:
                self._mark_error(local_account_id, exc.code)

        return self.get_user_status(user_id)

    async def refresh_user_live(self, user_id: UUID) -> Mt5ConnectionView:
        row = self._load_row(user_id)
        if row is None or str(row["account_environment"]) != "live":
            return self._empty_user_view()
        await self._refresh_row_day22(row)
        return self.get_user_status(user_id)

    def get_user_status(self, user_id: UUID) -> Mt5ConnectionView:
        row = self._load_row(user_id)
        if row is None or str(row["account_environment"]) != "live":
            return self._empty_user_view()
        return self._view_from_row(row)

    def _load_active_approval(self, user_id: UUID) -> Any | None:
        with self._session_factory() as session:
            return session.execute(
                text(
                    """
                    SELECT *
                    FROM mt5_account_approvals
                    WHERE user_id = :user_id
                      AND status = 'active'
                    LIMIT 1
                    """
                ),
                {"user_id": user_id},
            ).mappings().first()

    def _store_user_connection(
        self,
        *,
        user_id: UUID,
        token: str,
        login: str,
        server: str,
        remote: Any,
    ) -> UUID:
        ciphertext = self._cipher.encrypt(token)
        fingerprint = self._cipher.fingerprint(token)
        local_status = self._local_status(remote)
        now = datetime.now(UTC)
        with self._session_factory() as session:
            row_id = session.execute(
                text(
                    """
                    INSERT INTO mt5_accounts (
                        owner_user_id, broker, platform, account_environment,
                        login, server, metaapi_account_id,
                        metaapi_token_ciphertext, metaapi_token_fingerprint,
                        status, remote_state, remote_connection_status,
                        last_error_code, last_checked_at, last_connected_at, updated_at
                    ) VALUES (
                        :user_id, 'vantage', 'mt5', 'live',
                        :login, :server, :metaapi_account_id,
                        :ciphertext, :fingerprint,
                        :status, :remote_state, :remote_connection_status,
                        NULL, :now, :connected_at, :now
                    )
                    ON CONFLICT (owner_user_id)
                    DO UPDATE SET
                        broker = 'vantage',
                        platform = 'mt5',
                        account_environment = 'live',
                        login = EXCLUDED.login,
                        server = EXCLUDED.server,
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
                    "user_id": user_id,
                    "login": login,
                    "server": server,
                    "metaapi_account_id": remote.account_id,
                    "ciphertext": ciphertext,
                    "fingerprint": fingerprint,
                    "status": local_status,
                    "remote_state": remote.state,
                    "remote_connection_status": remote.connection_status,
                    "now": now,
                    "connected_at": now if local_status == "connected" else None,
                },
            ).scalar_one()
            session.add(
                AuditEvent(
                    actor_user_id=user_id,
                    event_type="mt5.user_connection_registered",
                    entity_type="mt5_account",
                    entity_id=row_id,
                    payload={
                        "broker": "vantage",
                        "platform": "mt5",
                        "account_environment": "live",
                        "login_last4": login[-4:],
                        "server": server,
                        "status": local_status,
                        "password_stored": False,
                        "token_encrypted": True,
                        "metaapi_supplied_by_user": False,
                        "trade_action_created": False,
                    },
                )
            )
            session.commit()
            return row_id

    def _audit_user_failure(
        self,
        user_id: UUID,
        error_code: str,
        *,
        retryable: bool = False,
    ) -> None:
        with self._session_factory() as session:
            session.add(
                AuditEvent(
                    actor_user_id=user_id,
                    event_type="mt5.user_connection_failed",
                    entity_type="mt5_account",
                    payload={
                        "error_code": error_code,
                        "retryable": retryable or error_code in self._TRANSIENT_CODES,
                        "password_stored": False,
                        "metaapi_supplied_by_user": False,
                        "trade_action_created": False,
                    },
                )
            )
            session.commit()

    @staticmethod
    def _empty_user_view() -> Mt5ConnectionView:
        return Mt5ConnectionView(
            configured=False,
            account_id=None,
            broker="vantage",
            platform="mt5",
            account_environment="live",
            login_masked=None,
            server=None,
            status="not_configured",
            remote_state=None,
            remote_connection_status=None,
            last_error_code=None,
            last_checked_at=None,
            last_connected_at=None,
        )
