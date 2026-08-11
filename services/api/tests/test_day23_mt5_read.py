from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.metaapi_read_gateway import MetaApiReadGateway
from app.mt5_read_service_day23 import (
    Day23AccountState,
    Day23LiveState,
    Day23Mt5ReadService,
    Day23PriceState,
    Day23ReadError,
)


def _service(max_age: float = 15.0) -> Day23Mt5ReadService:
    return Day23Mt5ReadService(
        session_factory=None,  # type: ignore[arg-type]
        cipher=None,  # type: ignore[arg-type]
        gateway=None,  # type: ignore[arg-type]
        max_quote_age_seconds=max_age,
    )


def _state(price: Day23PriceState) -> Day23LiveState:
    return Day23LiveState(
        local_account_id=uuid4(),
        metaapi_account_id="metaapi-account",
        login_masked="****1913",
        server="VantageMarkets-Demo",
        region="new-york",
        read_at=datetime.now(UTC),
        account=Day23AccountState(
            currency="USD",
            balance=10000,
            equity=10000,
            margin=0,
            free_margin=10000,
            margin_level=None,
            leverage=500,
            trade_allowed=True,
        ),
        price=price,
        positions=(),
        execution_ready=price.execution_ready,
        execution_block_reason=price.block_reason,
    )


def test_day23_read_gateway_has_no_trade_or_order_method() -> None:
    public_names = {
        name for name in dir(MetaApiReadGateway) if not name.startswith("_")
    }
    assert public_names == {
        "read_account_information",
        "read_positions",
        "read_symbol_price",
        "resolve_account_region",
    }


def test_buy_uses_ask_and_sell_uses_bid() -> None:
    now = datetime(2026, 8, 11, 8, 0, tzinfo=UTC)
    price = _service()._price_state(
        {
            "symbol": "XAUUSD",
            "bid": 3350.10,
            "ask": 3350.35,
            "time": now.isoformat(),
        },
        read_at=now,
    )
    state = _state(price)

    assert price.buy_price == price.ask == 3350.35
    assert price.sell_price == price.bid == 3350.10
    assert Day23Mt5ReadService.executable_price(state, "BUY") == 3350.35
    assert Day23Mt5ReadService.executable_price(state, "SELL") == 3350.10


def test_stale_quote_blocks_executable_price() -> None:
    now = datetime(2026, 8, 11, 8, 0, tzinfo=UTC)
    price = _service(max_age=10)._price_state(
        {
            "symbol": "XAUUSD",
            "bid": 3350.10,
            "ask": 3350.35,
            "time": (now - timedelta(seconds=11)).isoformat(),
        },
        read_at=now,
    )
    state = _state(price)

    assert price.stale is True
    assert price.execution_ready is False
    assert price.block_reason == "price_stale"
    with pytest.raises(Day23ReadError, match="price_stale"):
        Day23Mt5ReadService.executable_price(state, "BUY")


def test_unavailable_quote_blocks_executable_price() -> None:
    now = datetime(2026, 8, 11, 8, 0, tzinfo=UTC)
    price = _service()._price_state(
        {"symbol": "XAUUSD", "bid": None, "ask": 3350.35, "time": now.isoformat()},
        read_at=now,
    )
    state = _state(price)

    assert price.available is False
    assert price.execution_ready is False
    assert price.block_reason == "price_unavailable"
    with pytest.raises(Day23ReadError, match="price_unavailable"):
        Day23Mt5ReadService.executable_price(state, "SELL")
