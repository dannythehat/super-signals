"""Day 28 per-leg provider-zone guard for multi-TP market execution.

Day 26 authorises a zone signal using the current executable broker quote before the
first market order.  Day 28 live acceptance showed why the full route needs one more
check: Gold can move out of a narrow provider zone while several TP legs are being
submitted sequentially.

This module wraps the existing MetaAPI trade gateway.  For zone signals only, it reads
a fresh current-price immediately before *every* market-order request and refuses to
submit the next leg if BUY ask / SELL bid has left the provider's literal zone.  The
existing AtomicDay26 executor then rolls back any earlier leg from the same signal.

It does not change exact-entry logic, sizing, SL/TP values, provider targets or the
Day 25 gate.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_read_gateway import MetaApiReadGateway
from app.metaapi_trade_gateway import MetaApiTradeGateway
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
    ) -> None:
        self._base = base
        self._read = read_gateway
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
            if current < zone.low or current > zone.high:
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
    """Atomic Day 26 execution plus a fresh zone quote before every submitted leg."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        cipher: Any,
        read_gateway: MetaApiReadGateway,
        margin_gateway: Any,
        trade_gateway: MetaApiTradeGateway,
        zone_wait_seconds: float = 300.0,
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
            zone_wait_seconds=zone_wait_seconds,
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
