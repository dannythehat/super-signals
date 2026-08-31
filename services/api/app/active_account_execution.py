"""Canonical owner/reference execution against whichever MT5 account is active.

The historical Day-26 method names contain ``demo`` for compatibility, but the product
now has one canonical active MT5 slot that may be either Vantage Demo or Vantage Live.
The account switcher may replace that slot only while local Smart Signals exposure is
flat, so execution can safely remain single-account throughout the trading stack.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text

from app.graceful_market_targets import GracefulCaptureReliableCanonicalTradingExecutionService
from app.mt5_execution_day26 import Day26ExecutionError, _AccountInput, _SignalInput
from app.provider_risk_policy import provider_tp_limit


class ActiveAccountCanonicalTradingExecutionService(
    GracefulCaptureReliableCanonicalTradingExecutionService
):
    """Use the connected canonical MT5 slot regardless of Demo/Live environment."""

    def _load_active_account(self, user_id: UUID, signal_id: UUID) -> _AccountInput:
        with self._session_factory() as session:
            existing = int(
                session.execute(
                    text(
                        "SELECT COUNT(*) FROM positions WHERE signal_id=:signal_id AND user_id=:user_id"
                    ),
                    {"signal_id": signal_id, "user_id": user_id},
                ).scalar_one()
            )
            if existing:
                raise Day26ExecutionError("signal_execution_already_started")
            cancelled = bool(
                session.execute(
                    text(
                        """
                        SELECT EXISTS(
                            SELECT 1 FROM signal_lifecycle_events
                            WHERE signal_id=:signal_id AND event_type='cancel'
                        )
                        """
                    ),
                    {"signal_id": signal_id},
                ).scalar_one()
            )
            if cancelled:
                raise Day26ExecutionError("signal_cancelled")
            row = session.execute(
                text(
                    """
                    SELECT id,metaapi_account_id,metaapi_token_ciphertext,
                           account_environment,status
                    FROM mt5_accounts
                    WHERE owner_user_id=:user_id AND status!='revoked'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {"user_id": user_id},
            ).mappings().first()
        if row is None:
            raise Day26ExecutionError("mt5_account_not_configured")
        if str(row["account_environment"] or "").lower() not in {"demo", "live"}:
            raise Day26ExecutionError("mt5_account_environment_invalid")
        if str(row["status"] or "") != "connected":
            raise Day26ExecutionError("mt5_account_not_connected")
        return _AccountInput(
            local_account_id=UUID(str(row["id"])),
            metaapi_account_id=str(row["metaapi_account_id"]),
            token_ciphertext=bytes(row["metaapi_token_ciphertext"]),
        )

    # PaperCriticalExecutionService and bare-GOLD-NOW compatibility paths call this
    # method name. It intentionally means "load the canonical active account" now.
    def _load_demo_account(self, owner_user_id: UUID, signal_id: UUID) -> _AccountInput:
        return self._load_active_account(owner_user_id, signal_id)

    def _load_inputs(
        self,
        owner_user_id: UUID,
        signal_id: UUID,
    ) -> tuple[_SignalInput, _AccountInput]:
        bare = self._load_bare_now_signal(signal_id)
        if bare is not None:
            return bare, self._load_active_account(owner_user_id, signal_id)

        with self._session_factory() as session:
            signal_row = session.execute(
                text(
                    """
                    SELECT sig.id, sig.symbol, sig.side, sig.order_type,
                           sig.entry_low, sig.entry_high, sig.stop_loss,
                           sig.take_profits, sig.has_open_runner, sig.parser_status,
                           sig.risk_multiplier, sig.source_revision_index,
                           sig.source_posted_at,
                           COALESCE(src.source_alias, src.chat_title, '') AS source_name
                    FROM signals sig
                    LEFT JOIN sources src ON src.id = sig.source_id
                    WHERE sig.id = :signal_id
                    FOR UPDATE OF sig
                    """
                ),
                {"signal_id": signal_id},
            ).mappings().first()
            if signal_row is None:
                raise Day26ExecutionError("signal_not_found")
            if str(signal_row["parser_status"]) != "accepted":
                raise Day26ExecutionError("signal_not_accepted")
            if str(signal_row["order_type"]) != "market":
                raise Day26ExecutionError("day26_market_signal_required")

            existing_count = session.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM positions
                    WHERE signal_id = :signal_id AND user_id = :user_id
                    """
                ),
                {"signal_id": signal_id, "user_id": owner_user_id},
            ).scalar_one()
            if int(existing_count) != 0:
                raise Day26ExecutionError("signal_execution_already_started")

            cancelled = bool(
                session.execute(
                    text(
                        """
                        SELECT EXISTS(
                            SELECT 1
                            FROM signal_lifecycle_events
                            WHERE signal_id = :signal_id
                              AND event_type = 'cancel'
                        )
                        """
                    ),
                    {"signal_id": signal_id},
                ).scalar_one()
            )
            if cancelled:
                raise Day26ExecutionError("signal_cancelled")

            account_row = session.execute(
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
            if account_row is None:
                raise Day26ExecutionError("mt5_account_not_configured")
            if str(account_row["account_environment"] or "").lower() not in {"demo", "live"}:
                raise Day26ExecutionError("mt5_account_environment_invalid")
            if str(account_row["status"] or "") != "connected":
                raise Day26ExecutionError("mt5_account_not_connected")

        symbol = str(signal_row["symbol"] or "").strip().upper()
        side = str(signal_row["side"] or "").strip().upper()
        if symbol != "XAUUSD":
            raise Day26ExecutionError("day26_xauusd_required")
        if side not in {"BUY", "SELL"}:
            raise Day26ExecutionError("trade_side_invalid")

        entry_low = self._required_decimal(signal_row["entry_low"], "signal_entry_invalid")
        entry_high = self._required_decimal(signal_row["entry_high"], "signal_entry_invalid")
        if entry_high < entry_low:
            raise Day26ExecutionError("signal_entry_invalid")
        stop_loss = self._required_decimal(signal_row["stop_loss"], "signal_stop_loss_invalid")
        take_profits = self._take_profits(signal_row["take_profits"])
        has_open_runner = bool(signal_row["has_open_runner"])
        tp_limit = provider_tp_limit(
            source_name=str(signal_row["source_name"] or ""),
            side=side,
        )
        if tp_limit is not None:
            take_profits = take_profits[:tp_limit]
            has_open_runner = False
        if not self._directionally_valid(
            side=side,
            entry_low=entry_low,
            entry_high=entry_high,
            stop_loss=stop_loss,
            take_profits=take_profits,
        ):
            raise Day26ExecutionError("strict_directional_validation_failed")

        risk_multiplier = self._required_decimal(
            signal_row["risk_multiplier"], "signal_risk_multiplier_invalid"
        )
        source_posted_at = signal_row["source_posted_at"]
        if not isinstance(source_posted_at, datetime):
            raise Day26ExecutionError("signal_posted_at_invalid")

        return (
            _SignalInput(
                signal_id=signal_id,
                symbol=symbol,
                side=side,
                entry_low=entry_low,
                entry_high=entry_high,
                stop_loss=stop_loss,
                take_profits=take_profits,
                has_open_runner=has_open_runner,
                signal_requests_double_lot=risk_multiplier > Decimal("1"),
                source_revision_index=int(signal_row["source_revision_index"]),
                source_posted_at=source_posted_at,
            ),
            _AccountInput(
                local_account_id=UUID(str(account_row["id"])),
                metaapi_account_id=str(account_row["metaapi_account_id"]),
                token_ciphertext=bytes(account_row["metaapi_token_ciphertext"]),
            ),
        )


__all__ = ["ActiveAccountCanonicalTradingExecutionService"]
