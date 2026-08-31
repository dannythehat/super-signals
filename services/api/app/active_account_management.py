"""Canonical management against the user's one active Demo or Real MT5 account."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text

from app.mt5_management_day27 import Day27ManagementError, _Account
from app.trading_management_canonical import CanonicalTradingManagementService


class ActiveAccountCanonicalTradingManagementService(CanonicalTradingManagementService):
    """Keep all provider management on the exact account selected as active."""

    def _load_account(self, owner_user_id: UUID) -> _Account | None:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT id, metaapi_account_id, metaapi_token_ciphertext,
                           account_environment, status
                    FROM mt5_accounts
                    WHERE owner_user_id = :owner_user_id
                      AND status != 'revoked'
                    LIMIT 1
                    """
                ),
                {"owner_user_id": owner_user_id},
            ).mappings().first()
        if row is None:
            return None
        if str(row["account_environment"] or "").lower() not in {"demo", "live"}:
            raise Day27ManagementError("mt5_account_environment_invalid")
        if str(row["status"] or "") != "connected":
            raise Day27ManagementError("mt5_account_not_connected", retryable=True)
        return _Account(
            local_id=UUID(str(row["id"])),
            account_id=str(row["metaapi_account_id"]),
            token_ciphertext=bytes(row["metaapi_token_ciphertext"]),
        )


__all__ = ["ActiveAccountCanonicalTradingManagementService"]
