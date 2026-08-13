"""Day 38 provider-management boundary for ordinary member LIVE accounts."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text

from app.mt5_management_day27 import Day27ManagementError, Day27Mt5ManagementService, _Account


class Day38LiveUserManagementService(Day27Mt5ManagementService):
    """Reuse Day 27 mapped-only management with a LIVE approved-account gate."""

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
                {"user_id": user_id},
            ).mappings().first()
        if row is None:
            return None
        if str(row["account_environment"] or "").lower() != "live":
            raise Day27ManagementError("day38_live_account_required")
        if str(row["status"] or "") != "connected":
            raise Day27ManagementError("mt5_account_not_connected", retryable=True)
        if str(row["login"] or "") != str(row["approved_login"] or ""):
            raise Day27ManagementError("mt5_account_not_approved")
        if str(row["server"] or "").strip().lower() != str(
            row["approved_server"] or ""
        ).strip().lower():
            raise Day27ManagementError("mt5_account_not_approved")
        return _Account(
            local_id=UUID(str(row["id"])),
            account_id=str(row["metaapi_account_id"]),
            token_ciphertext=bytes(row["metaapi_token_ciphertext"]),
        )


__all__ = ["Day38LiveUserManagementService"]
