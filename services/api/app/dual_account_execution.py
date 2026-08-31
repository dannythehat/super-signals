"""Active Paper/Real account adapters for ordinary-member execution and management."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text

from app.graceful_market_targets import GracefulCaptureReliableMemberTradingExecutionService
from app.mt5_execution_day26 import Day26ExecutionError
from app.mt5_management_day27 import Day27ManagementError, _Account
from app.trading_management_canonical import MemberTradingManagementService


class DualAccountMemberTradingExecutionService(
    GracefulCaptureReliableMemberTradingExecutionService
):
    """Use the member's explicitly selected connected account for new signals."""

    @staticmethod
    def _live_account_row(session, user_id: UUID):  # noqa: ANN001
        return session.execute(
            text(
                """
                SELECT
                    m.id AS mt5_account_id,
                    m.metaapi_account_id,
                    m.metaapi_token_ciphertext,
                    m.account_environment,
                    m.status AS mt5_status,
                    m.login,
                    m.server,
                    a.login AS approved_login,
                    a.server AS approved_server
                FROM users AS u
                JOIN user_roles AS ur ON ur.user_id=u.id
                JOIN roles AS r ON r.id=ur.role_id AND r.name='user'
                JOIN user_trading_controls AS utc ON utc.user_id=u.id
                JOIN mt5_accounts AS m
                  ON m.owner_user_id=u.id
                 AND m.account_environment=utc.active_account_environment
                LEFT JOIN mt5_account_approvals AS a
                  ON a.user_id=u.id
                 AND a.status='active'
                WHERE u.id=:user_id
                  AND u.status='active'
                  AND utc.trading_status='active'
                  AND utc.active_account_environment IN ('demo','live')
                  AND m.status!='revoked'
                LIMIT 1
                """
            ),
            {"user_id": user_id},
        ).mappings().first()

    @staticmethod
    def _validate_live_account_row(row) -> None:  # noqa: ANN001
        environment = str(row["account_environment"] or "").lower()
        if environment not in {"demo", "live"}:
            raise Day26ExecutionError("day38_user_not_execution_ready")
        if str(row.get("mt5_status") or "") != "connected":
            raise Day26ExecutionError("mt5_account_not_connected")
        if environment == "demo":
            return
        if str(row["login"] or "") != str(row["approved_login"] or ""):
            raise Day26ExecutionError("mt5_account_not_approved")
        if str(row["server"] or "").strip().lower() != str(
            row["approved_server"] or ""
        ).strip().lower():
            raise Day26ExecutionError("mt5_account_not_approved")


class DualAccountMemberTradingManagementService(MemberTradingManagementService):
    """Manage existing exposure on the currently selected account.

    The active-account endpoint refuses environment changes while unfinished Smart
    Signals positions exist, so this lookup remains bound to the account that opened
    those positions for their full lifecycle.
    """

    def _load_account(self, user_id: UUID) -> _Account | None:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT
                        m.id,
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
                    JOIN user_trading_controls AS utc ON utc.user_id=u.id
                    JOIN mt5_accounts AS m
                      ON m.owner_user_id=u.id
                     AND m.account_environment=utc.active_account_environment
                    LEFT JOIN mt5_account_approvals AS a
                      ON a.user_id=u.id
                     AND a.status='active'
                    WHERE u.id=:user_id
                      AND u.status='active'
                      AND utc.active_account_environment IN ('demo','live')
                      AND m.status!='revoked'
                    LIMIT 1
                    """
                ),
                {"user_id": user_id},
            ).mappings().first()
        if row is None:
            return None
        environment = str(row["account_environment"] or "").lower()
        if str(row["status"] or "") != "connected":
            raise Day27ManagementError("mt5_account_not_connected", retryable=True)
        if environment == "live":
            if str(row["login"] or "") != str(row["approved_login"] or ""):
                raise Day27ManagementError("mt5_account_not_approved")
            if str(row["server"] or "").strip().lower() != str(
                row["approved_server"] or ""
            ).strip().lower():
                raise Day27ManagementError("mt5_account_not_approved")
        elif environment != "demo":
            raise Day27ManagementError("mt5_account_not_configured")
        return _Account(
            local_id=UUID(str(row["id"])),
            account_id=str(row["metaapi_account_id"]),
            token_ciphertext=bytes(row["metaapi_token_ciphertext"]),
        )


def active_environment(session_factory, user_id: UUID) -> str | None:  # noqa: ANN001
    with session_factory() as session:
        value = session.execute(
            text(
                """
                SELECT active_account_environment
                FROM user_trading_controls
                WHERE user_id=:user_id
                """
            ),
            {"user_id": user_id},
        ).scalar_one_or_none()
    normalized = str(value or "").lower()
    return normalized if normalized in {"demo", "live"} else None


__all__ = [
    "DualAccountMemberTradingExecutionService",
    "DualAccountMemberTradingManagementService",
    "active_environment",
]
