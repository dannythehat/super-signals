from __future__ import annotations

from decimal import Decimal

import pytest

from app.day28_zone_guard import Day28ZoneGuardTradeGateway
from app.metaapi_gateway import MetaApiGatewayError


class FakeReadGateway:
    def __init__(self, prices: list[dict[str, object]]) -> None:
        self.prices = list(prices)
        self.calls = 0

    async def read_symbol_price(self, **kwargs):
        self.calls += 1
        return self.prices.pop(0)


class FakeTradeGateway:
    def __init__(self) -> None:
        self.market_calls: list[dict] = []

    async def place_market_order(self, **kwargs):
        self.market_calls.append(dict(kwargs))
        return object()

    async def close_position(self, **kwargs):
        return None

    async def modify_position(self, **kwargs):
        return None

    async def cancel_order(self, **kwargs):
        return None


ORDER = {
    "token": "token",
    "account_id": "account",
    "region": "london",
    "side": "BUY",
    "symbol": "XAUUSD",
    "volume": 0.01,
    "stop_loss": 95.0,
    "take_profit": 105.0,
    "client_id": "SS_D28_ABC",
}


@pytest.mark.asyncio
async def test_zone_is_admitted_once_for_atomic_multi_tp_batch() -> None:
    """Sibling TP positions are one execution decision, not repeated zone decisions."""
    read = FakeReadGateway([
        {"ask": 100.00, "bid": 99.90},
        # If the old per-leg guard were still active this second value would abort
        # the signal after TP1 and trigger a rollback.
        {"ask": 101.25, "bid": 101.15},
    ])
    base = FakeTradeGateway()
    guarded = Day28ZoneGuardTradeGateway(base=base, read_gateway=read)
    token = guarded.set_zone(Decimal("99.50"), Decimal("100.50"))
    try:
        await guarded.place_market_order(**ORDER)
        await guarded.place_market_order(**{**ORDER, "client_id": "SS_D28_DEF"})
    finally:
        guarded.reset_zone(token)

    assert read.calls == 1
    assert len(base.market_calls) == 2


@pytest.mark.asyncio
async def test_sell_uses_bid_for_zone_gate() -> None:
    read = FakeReadGateway([{"ask": 100.80, "bid": 100.40}])
    base = FakeTradeGateway()
    guarded = Day28ZoneGuardTradeGateway(base=base, read_gateway=read)
    token = guarded.set_zone(Decimal("100.00"), Decimal("100.50"))
    try:
        await guarded.place_market_order(**{**ORDER, "side": "SELL"})
    finally:
        guarded.reset_zone(token)

    assert len(base.market_calls) == 1
    assert read.calls == 1


@pytest.mark.asyncio
async def test_exact_entry_path_is_not_changed_by_day28_zone_guard() -> None:
    read = FakeReadGateway([])
    base = FakeTradeGateway()
    guarded = Day28ZoneGuardTradeGateway(base=base, read_gateway=read)

    await guarded.place_market_order(**ORDER)

    assert read.calls == 0
    assert len(base.market_calls) == 1


@pytest.mark.asyncio
async def test_marginal_tick_outside_the_zone_still_submits() -> None:
    """One ordinary tick past the zone edge must not cancel the whole signal.

    A live TDC zone trade was lost this way: price entered the zone, the engine
    spent a few hundred milliseconds on preflight, and the pre-submission check
    found price a fraction outside the edge and refused every leg. A human
    following the same signal clicks market and takes the fill.
    """
    read = FakeReadGateway([{"ask": 100.70, "bid": 100.60}])
    base = FakeTradeGateway()
    guarded = Day28ZoneGuardTradeGateway(
        base=base, read_gateway=read, tolerance=Decimal("0.50")
    )
    token = guarded.set_zone(Decimal("99.50"), Decimal("100.50"))
    try:
        await guarded.place_market_order(**ORDER)
    finally:
        guarded.reset_zone(token)

    # 100.70 is 0.20 above the zone high, inside the 0.50 tolerance.
    assert len(base.market_calls) == 1


@pytest.mark.asyncio
async def test_genuine_departure_from_the_zone_is_still_refused_before_first_leg() -> None:
    """The initial fresh guard still exists; only sibling-leg re-decisions are removed."""
    read = FakeReadGateway([{"ask": 101.60, "bid": 101.50}])
    base = FakeTradeGateway()
    guarded = Day28ZoneGuardTradeGateway(
        base=base, read_gateway=read, tolerance=Decimal("0.50")
    )
    token = guarded.set_zone(Decimal("99.50"), Decimal("100.50"))
    try:
        with pytest.raises(MetaApiGatewayError, match="zone_left_before_position_submission"):
            await guarded.place_market_order(**ORDER)
    finally:
        guarded.reset_zone(token)

    assert base.market_calls == []


@pytest.mark.asyncio
async def test_zero_tolerance_restores_strict_zone_containment() -> None:
    read = FakeReadGateway([{"ask": 100.70, "bid": 100.60}])
    base = FakeTradeGateway()
    guarded = Day28ZoneGuardTradeGateway(
        base=base, read_gateway=read, tolerance=Decimal("0")
    )
    token = guarded.set_zone(Decimal("99.50"), Decimal("100.50"))
    try:
        with pytest.raises(MetaApiGatewayError, match="zone_left_before_position_submission"):
            await guarded.place_market_order(**ORDER)
    finally:
        guarded.reset_zone(token)

    assert base.market_calls == []
