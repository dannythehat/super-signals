"""Day 28 per-leg provider-zone guard for multi-TP market execution.

Automatic execution samples a provider zone once when the fresh signal arrives. It does
not wait for price to come back later, chase the setup, or retry a stale entry. If the
current executable price is not in the provider zone the trade is refused immediately.

For a zone that is executable now, this module still performs one fresh price check
immediately before each market-order leg so a material move outside the provider's
literal zone cannot create later legs at a stale price. The approved bounded entry
tolerance is honoured at the edge.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_read_gateway import MetaApiReadGateway
from app.metaapi_trade_gateway import MetaApiTradeGateway
from app.mt5_execution_day26 import (
    Day26ExecutionError,
    _AccountInput,
    _SignalInput,
    _resolve_entry_tolerance,
)
from app.mt5_execution_day26_atomic import AtomicDay26Mt5ExecutionService


@dataclass(frozen=True, slots=True)
class _Zone:
    low: Decimal
    high: Decimal


class Day28ZoneGuardTradeGateway:
    """Delegate all trade mutations, guarding only zone market-order submission."""

    def __init__(
        self,
        *,
        base: MetaApiTradeGateway,
        read_gateway: MetaApiReadGateway,
        tolerance: Decimal | str | None = None,
    ) -> None:
        self._base = base
        self._read = read_gateway
        self._tolerance = _resolve_entry_tolerance(tolerance)
        self._zone: ContextVar[_Zone | None] = ContextVar("day28_provider_zone", default=None)

    def set_zone(self, low: Decimal, high: Decimal) -> Token[_Zone | None]:
        if low <= 0 or high < low:
            raise ValueError("day28_provider_zone_invalid")
        return self._zone.set(_Zone(low=low, high=high))

    def reset_zone(self, token: Token[_Zone | None]) -> None:
        self._zone.reset(token)

    async def place_market_order(self, **kwargs: Any):
        zone = self._zone.get()
        if zone is not None:
            payload = await self._read.read_symbol_price(
                token=str(kwargs["token"]),
                account_id=str(kwargs["account_id"]),
                region=str(kwargs["region"]),
                symbol=str(kwargs["symbol"]),
            )
            side = str(kwargs["side"]).strip().upper()
            field = "ask" if side == "BUY" else "bid" if side == "SELL" else None
            if field is None:
                raise MetaApiGatewayError("trade_side_invalid")
            current = self._positive_decimal(payload.get(field))
            if current is None:
                raise MetaApiGatewayError("zone_submission_price_unavailable", retryable=True)
            if current < zone.low - self._tolerance or current > zone.high + self._tolerance:
                raise MetaApiGatewayError("zone_left_before_position_submission")
        return await self._base.place_market_order(**kwargs)

    async def close_position(self, **kwargs: Any) -> None:
        await self._base.close_position(**kwargs)

    async def modify_position(self, **kwargs: Any) -> None:
        await self._base.modify_position(**kwargs)

    async def cancel_order(self, **kwargs: Any) -> None:
        await self._base.cancel_order(**kwargs)

    @staticmethod
    def _positive_decimal(value: Any) -> Decimal | None:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return None
        if not parsed.is_finite() or parsed <= 0:
            return None
        return parsed


class Day28GuardedExecutionService(AtomicDay26Mt5ExecutionService):
    """Atomic execution with no delayed zone chase and a fresh per-leg zone guard."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        cipher: Any,
        read_gateway: MetaApiReadGateway,
        margin_gateway: Any,
        trade_gateway: MetaApiTradeGateway,
        zone_wait_seconds: float = 0.0,
        zone_poll_seconds: float = 2.0,
    ) -> None:
        self._day28_session_factory = session_factory
        self._day28_guard = Day28ZoneGuardTradeGateway(
            base=trade_gateway,
            read_gateway=read_gateway,
        )
        super().__init__(
            session_factory=session_factory,
            cipher=cipher,
            read_gateway=read_gateway,
            margin_gateway=margin_gateway,
            trade_gateway=self._day28_guard,
            zone_wait_seconds=0.0,
            zone_poll_seconds=zone_poll_seconds,
        )

    async def execute_owner_demo_signal(self, **kwargs: Any):
        signal_id = kwargs.get("signal_id")
        if signal_id is None:
            return await super().execute_owner_demo_signal(**kwargs)
        low, high = self._provider_zone(signal_id)
        token: Token[_Zone | None] | None = None
        if low != high:
            token = self._day28_guard.set_zone(low, high)
        try:
            return await super().execute_owner_demo_signal(**kwargs)
        finally:
            if token is not None:
                self._day28_guard.reset_zone(token)

    def _load_inputs(
        self,
        owner_user_id: UUID,
        signal_id: UUID,
    ) -> tuple[_SignalInput, _AccountInput]:
        """Owner paper inputs without a stale cached-connection veto.

        The account must exist, be demo and not be revoked. The live Day 23 read that
        immediately follows is responsible for proving whether MetaAPI/MT5 is actually
        reachable and tradeable now.
        """
        with self._session_factory() as session:
            signal_row = session.execute(
                text(
                    """
                    SELECT id, symbol, side, order_type, entry_low, entry_high,
                           stop_loss, take_profits, has_open_runner, parser_status,
                           risk_multiplier, source_revision_index, source_posted_at
                    FROM signals
                    WHERE id = :signal_id
                    FOR UPDATE
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
                           account_environment
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
            if str(account_row["account_environment"]).lower() != "demo":
                raise Day26ExecutionError("day26_demo_account_required")

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
        if source_posted_at is None:
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
                local_account_id=account_row["id"],
                metaapi_account_id=str(account_row["metaapi_account_id"]),
                token_ciphertext=bytes(account_row["metaapi_token_ciphertext"]),
            ),
        )

    def _provider_zone(self, signal_id: Any) -> tuple[Decimal, Decimal]:
        with self._day28_session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT entry_low, entry_high
                    FROM signals
                    WHERE id = :signal_id
                    LIMIT 1
                    """
                ),
                {"signal_id": signal_id},
            ).mappings().first()
        if row is None:
            raise ValueError("day28_signal_not_found")
        low = Decimal(str(row["entry_low"]))
        high = Decimal(str(row["entry_high"]))
        if low <= 0 or high < low:
            raise ValueError("day28_provider_zone_invalid")
        return low, high
