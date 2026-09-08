"""Trading-control adapter for members with both Paper and Real MT5 accounts."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text

from app.trading_controls_day31 import Day31TradingControlService, _Account


class DualAccountTradingControlService(Day31TradingControlService):
    """Validate and stop the account explicitly selected by the member."""

    def _activation_requirements(self, user_id: UUID) -> tuple[dict[str, object], ...]:
        with self._session_factory() as session:
            user_active = bool(
                session.scalar(
                    text("SELECT EXISTS(SELECT 1 FROM users WHERE id=:id AND status='active')"),
                    {"id": user_id},
                )
            )
            control = session.execute(
                text(
                    """
                    SELECT active_account_environment
                    FROM user_trading_controls
                    WHERE user_id=:user_id
                    LIMIT 1
                    """
                ),
                {"user_id": user_id},
            ).mappings().first()
            environment = str(
                control["active_account_environment"] if control is not None else ""
            ).lower()
            account = session.execute(
                text(
                    """
                    SELECT login, server, status, account_environment
                    FROM mt5_accounts
                    WHERE owner_user_id=:user_id
                      AND account_environment=:environment
                      AND status!='revoked'
                    LIMIT 1
                    """
                ),
                {"user_id": user_id, "environment": environment},
            ).mappings().first()
            approval = session.execute(
                text(
                    """
                    SELECT login, server
                    FROM mt5_account_approvals
                    WHERE user_id=:user_id AND status='active'
                    LIMIT 1
                    """
                ),
                {"user_id": user_id},
            ).mappings().first()

        selected = environment in {"demo", "live"}
        connected = bool(account and str(account["status"]) == "connected")
        live_approved = bool(
            environment == "live"
            and account
            and approval
            and str(account["login"]) == str(approval["login"])
            and str(account["server"]).casefold() == str(approval["server"]).casefold()
        )
        account_ready = connected and (environment == "demo" or live_approved)
        label = (
            "Selected Paper MT5 account connected"
            if environment == "demo"
            else "Selected Real MT5 account approved and connected"
        )
        return (
            {"key": "active_account", "label": "Smart Signals account active", "passed": user_active},
            {"key": "trade_account_selected", "label": "Paper or Real account selected", "passed": selected},
            {"key": "mt5_connected", "label": label, "passed": account_ready},
            {"key": "risk_selected", "label": "Risk settings selected", "passed": True},
        )

    def _connected_live_account(self, user_id: UUID) -> _Account | None:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT m.id, m.metaapi_account_id, m.login, m.server,
                           m.metaapi_token_ciphertext, m.account_environment,
                           a.login AS approved_login, a.server AS approved_server
                    FROM user_trading_controls AS utc
                    JOIN mt5_accounts AS m
                      ON m.owner_user_id=utc.user_id
                     AND m.account_environment=utc.active_account_environment
                    LEFT JOIN mt5_account_approvals AS a
                      ON a.user_id=utc.user_id
                     AND a.status='active'
                    WHERE utc.user_id=:user_id
                      AND utc.active_account_environment IN ('demo','live')
                      AND m.status='connected'
                    LIMIT 1
                    """
                ),
                {"user_id": user_id},
            ).mappings().first()
        if row is None:
            return None
        environment = str(row["account_environment"] or "").lower()
        if environment == "live":
            if str(row["login"] or "") != str(row["approved_login"] or ""):
                return None
            if str(row["server"] or "").casefold() != str(row["approved_server"] or "").casefold():
                return None
        return _Account(
            local_id=row["id"],
            account_id=str(row["metaapi_account_id"]),
            login=str(row["login"]),
            server=str(row["server"]),
            token_ciphertext=bytes(row["metaapi_token_ciphertext"]),
        )


__all__ = ["DualAccountTradingControlService"]
