"""Day 38 live-account execution boundary for ordinary invited users.

The Owner keeps the accepted demo-only Day 28 path. Ordinary members use the same
atomic execution + per-leg provider-zone guard, but their account gate is LIVE-only.
Stored Day 31 risk/double-lot choices are loaded server-side and cannot be supplied
by the caller.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text

from app.day28_zone_guard import Day28GuardedExecutionService
from app.mt5_execution_day26 import (
    Day26ExecutionError,
    Day26ExecutionResult,
    _AccountInput,
    _SignalInput,
)


class Day38LiveUserExecutionService(Day28GuardedExecutionService):
    """Run the strongest accepted execution engine against one member LIVE account."""

    async def execute_live_user_signal(
        self,
        *,
        user_id: UUID,
        signal_id: UUID,
    ) -> Day26ExecutionResult:
        risk_percent, allow_double_lot = self._load_live_preferences(user_id)
        return await self.execute_owner_demo_signal(
            owner_user_id=user_id,
            signal_id=signal_id,
            risk_percent=risk_percent,
            double_lot_approved=allow_double_lot,
        )

    def _load_live_preferences(self, user_id: UUID) -> tuple[str, bool]:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT utc.risk_percent, utc.allow_double_lot
                    FROM users AS u
                    JOIN user_roles AS ur ON ur.user_id=u.id
                    JOIN roles AS r ON r.id=ur.role_id AND r.name='user'
                    JOIN user_trading_controls AS utc ON utc.user_id=u.id
                    WHERE u.id=:user_id
                      AND u.status='active'
                      AND utc.trading_status='active'
                    LIMIT 1
                    """
                ),
                {"user_id": user_id},
            ).mappings().first()
        if row is None:
            raise Day26ExecutionError("day38_user_not_execution_active")
        return str(row["risk_percent"]), bool(row["allow_double_lot"])

    def _load_inputs(self, user_id: UUID, signal_id: UUID) -> tuple[_SignalInput, _AccountInput]:
        """Accepted Day 26 input contract with the ordinary-member LIVE gate."""
        with self._session_factory() as session:
            signal_row = session.execute(
                text(
                    """
                    SELECT
                        id,
                        symbol,
                        side,
                        entry_low,
                        entry_high,
                        stop_loss,
                        take_profits,
                        canonical_payload,
                        parser_status,
                        order_type,
                        source_revision_index,
                        created_at
                    FROM signals
                    WHERE id=:signal_id
                    LIMIT 1
                    """
                ),
                {"signal_id": signal_id},
            ).mappings().first()
            if signal_row is None:
                raise Day26ExecutionError("signal_not_found")
            if str(signal_row["parser_status"] or "") != "accepted":
                raise Day26ExecutionError("signal_not_accepted")
            if str(signal_row["order_type"] or "").lower() != "market":
                raise Day26ExecutionError("day26_pending_order_unsupported")

            existing_positions = int(
                session.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM positions
                        WHERE signal_id=:signal_id AND user_id=:user_id
                        """
                    ),
                    {"signal_id": signal_id, "user_id": user_id},
                ).scalar_one()
            )
            if existing_positions > 0:
                raise Day26ExecutionError("signal_execution_already_started")

            cancelled = session.execute(
                text(
                    """
                    SELECT 1
                    FROM signal_lifecycle_events
                    WHERE signal_id=:signal_id
                      AND event_type='cancelled'
                    LIMIT 1
                    """
                ),
                {"signal_id": signal_id},
            ).scalar_one_or_none()
            if cancelled:
                raise Day26ExecutionError("signal_cancelled_before_execution")

            eligibility = session.execute(
                text(
                    """
                    SELECT
                        m.id AS mt5_account_id,
                        m.metaapi_account_id,
                        m.metaapi_token_ciphertext,
                        m.account_environment,
                        m.status AS account_status,
                        m.login,
                        m.server,
                        a.login AS approved_login,
                        a.server AS approved_server
                    FROM users AS u
                    JOIN user_roles AS ur ON ur.user_id=u.id
                    JOIN roles AS r ON r.id=ur.role_id AND r.name='user'
                    JOIN user_trading_controls AS utc ON utc.user_id=u.id
                    JOIN mt5_accounts AS m ON m.owner_user_id=u.id
                    JOIN mt5_account_approvals AS a
                      ON a.user_id=u.id
                     AND a.status='active'
                    WHERE u.id=:user_id
                      AND u.status='active'
                      AND utc.trading_status='active'
                      AND m.status!='revoked'
                    ORDER BY m.created_at DESC
                    LIMIT 1
                    """
                ),
                {"user_id": user_id},
            ).mappings().first()
            if eligibility is None:
                raise Day26ExecutionError("day38_user_not_execution_ready")
            if str(eligibility["account_environment"] or "").lower() != "live":
                raise Day26ExecutionError("day38_live_account_required")
            if str(eligibility["account_status"] or "") != "connected":
                raise Day26ExecutionError("mt5_account_not_connected")
            if str(eligibility["login"] or "") != str(eligibility["approved_login"] or ""):
                raise Day26ExecutionError("mt5_account_not_approved")
            if str(eligibility["server"] or "").strip().lower() != str(
                eligibility["approved_server"] or ""
            ).strip().lower():
                raise Day26ExecutionError("mt5_account_not_approved")

        payload = signal_row["canonical_payload"] if isinstance(signal_row["canonical_payload"], dict) else {}
        has_open_runner = any(
            isinstance(item, str) and item.strip().upper() == "OPEN"
            for item in (payload.get("take_profits") or [])
        )
        double_lot_requested = bool(payload.get("double_lot_requested", False))
        take_profits = self._take_profits(signal_row["take_profits"])
        source_posted_at = signal_row["created_at"]
        if source_posted_at is None:
            raise Day26ExecutionError("signal_created_at_missing")

        signal = _SignalInput(
            signal_id=UUID(str(signal_row["id"])),
            symbol=str(signal_row["symbol"] or "").upper(),
            side=str(signal_row["side"] or "").lower(),
            entry_low=self._required_decimal(signal_row["entry_low"], "entry_low_missing"),
            entry_high=self._required_decimal(signal_row["entry_high"], "entry_high_missing"),
            stop_loss=self._required_decimal(signal_row["stop_loss"], "stop_loss_missing"),
            take_profits=take_profits,
            has_open_runner=has_open_runner,
            signal_requests_double_lot=double_lot_requested,
            source_revision_index=int(signal_row["source_revision_index"]),
            source_posted_at=source_posted_at,
        )
        if signal.symbol != "XAUUSD":
            raise Day26ExecutionError("day26_symbol_unsupported")
        if signal.side not in {"buy", "sell"}:
            raise Day26ExecutionError("day26_side_invalid")
        if signal.position_count < 1:
            raise Day26ExecutionError("day26_take_profit_required")
        if not self._directionally_valid(signal):
            raise Day26ExecutionError("day26_signal_prices_invalid")

        account = _AccountInput(
            local_account_id=UUID(str(eligibility["mt5_account_id"])),
            metaapi_account_id=str(eligibility["metaapi_account_id"]),
            token_ciphertext=bytes(eligibility["metaapi_token_ciphertext"]),
        )
        return signal, account


__all__ = ["Day38LiveUserExecutionService"]
