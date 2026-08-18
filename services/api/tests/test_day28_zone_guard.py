from __future__ import annotations

from decimal import Decimal

import pytest

from app.day28_zone_guard import Day28ZoneGuardTradeGateway


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
async def test_fresh_market_batch_is_not_redecided_by_provider_zone() -> None:
    """A fresh market signal is one broker submission decision, not another zone gate."""
    read = FakeReadGateway([
        {"ask": 100.00, "bid": 99.90},
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

    assert read.calls == 0
    assert len(base.market_calls) == 2


@pytest.mark.asyncio
async def test_sell_market_signal_also_reaches_broker_without_local_zone_read() -> None:
    read = FakeReadGateway([{"ask": 100.80, "bid": 100.40}])
    base = FakeTradeGateway()
    guarded = Day28ZoneGuardTradeGateway(base=base, read_gateway=read)
    token = guarded.set_zone(Decimal("100.00"), Decimal("100.50"))
    try:
        await guarded.place_market_order(**{**ORDER, "side": "SELL"})
    finally:
        guarded.reset_zone(token)

    assert len(base.market_calls) == 1
    assert read.calls == 0


@pytest.mark.asyncio
async def test_exact_entry_path_is_not_changed_by_day28_zone_guard() -> None:
    read = FakeReadGateway([])
    base = FakeTradeGateway()
    guarded = Day28ZoneGuardTradeGateway(base=base, read_gateway=read)

    await guarded.place_market_order(**ORDER)

    assert read.calls == 0
    assert len(base.market_calls) == 1


@pytest.mark.asyncio
async def test_market_signal_just_outside_text_range_still_reaches_mt5() -> None:
    """Provider range is evidence; fresh market submission is broker-authoritative."""
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

    assert read.calls == 0
    assert len(base.market_calls) == 1


@pytest.mark.asyncio
async def test_large_market_departure_is_not_a_super_signals_veto() -> None:
    """Regression for United Kings 28739: MT5, not our range guard, decides the order."""
    read = FakeReadGateway([{"ask": 101.60, "bid": 101.50}])
    base = FakeTradeGateway()
    guarded = Day28ZoneGuardTradeGateway(
        base=base, read_gateway=read, tolerance=Decimal("0.50")
    )
    token = guarded.set_zone(Decimal("99.50"), Decimal("100.50"))
    try:
        await guarded.place_market_order(**ORDER)
    finally:
        guarded.reset_zone(token)

    assert read.calls == 0
    assert len(base.market_calls) == 1


@pytest.mark.asyncio
async def test_zone_tolerance_no_longer_controls_fresh_market_submission() -> None:
    """Tolerance cannot silently become another trade blocker for MARKET instructions."""
    read = FakeReadGateway([{"ask": 100.70, "bid": 100.60}])
    base = FakeTradeGateway()
    guarded = Day28ZoneGuardTradeGateway(
        base=base, read_gateway=read, tolerance=Decimal("0")
    )
    token = guarded.set_zone(Decimal("99.50"), Decimal("100.50"))
    try:
        await guarded.place_market_order(**ORDER)
    finally:
        guarded.reset_zone(token)

    assert read.calls == 0
    assert len(base.market_calls) == 1
