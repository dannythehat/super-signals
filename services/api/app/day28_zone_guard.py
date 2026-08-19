"""Compatibility execution adapter with no local market-zone veto.

A fresh provider MARKET instruction is submitted using the shared execution policy. The
provider's entry/range remains canonical evidence, but this adapter never re-reads price
between TP tranches and never blocks a broker mutation because price moved outside that
text range. Vantage/MT5 remains authoritative for broker acceptance.

This module exists only while the remaining day-numbered callers are migrated to the
canonical executor; it contains no independent trading policy.
"""

from __future__ import annotations

from contextvars import Token
from decimal import Decimal
from typing import Any

from app.metaapi_trade_gateway import MetaApiTradeGateway
from app.mt5_execution_day26_atomic import AtomicDay26Mt5ExecutionService


class Day28ZoneGuardTradeGateway:
    """Pass-through compatibility wrapper; MARKET orders are never locally zone-gated."""

    def __init__(
        self,
        *,
        base: MetaApiTradeGateway,
        read_gateway: Any,
        tolerance: Decimal | str | None = None,
    ) -> None:
        del read_gateway, tolerance
        self._base = base

    @staticmethod
    def set_zone(low: Decimal, high: Decimal) -> None:
        del low, high
        return None

    @staticmethod
    def reset_zone(token: Token | None) -> None:
        del token

    async def place_market_order(self, **kwargs: Any):
        return await self._base.place_market_order(**kwargs)

    async def close_position(self, **kwargs: Any):
        return await self._base.close_position(**kwargs)

    async def modify_position(self, **kwargs: Any):
        return await self._base.modify_position(**kwargs)

    async def cancel_order(self, **kwargs: Any):
        return await self._base.cancel_order(**kwargs)


class Day28GuardedExecutionService(AtomicDay26Mt5ExecutionService):
    """Compatibility name for the canonical atomic market executor."""


__all__ = ["Day28GuardedExecutionService", "Day28ZoneGuardTradeGateway"]
