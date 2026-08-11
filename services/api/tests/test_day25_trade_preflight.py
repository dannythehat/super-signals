import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import httpx

from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_margin_gateway import MetaApiMarginGateway
from app.mt5_read_service_day23 import Day23AccountState, Day23LiveState, Day23PriceState
from app.risk_sizing_day24 import BrokerVolumeRules, Day24RiskSizer
from app.trade_preflight_day25 import Day25TradePreflightService


class FakeMarginGateway:
    def __init__(self, margin: float = 100.0, *, error: bool = False) -> None:
        self.margin = margin
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def calculate_margin(self, **kwargs: object) -> float:
        self.calls.append(kwargs)
        if self.error:
            raise MetaApiGatewayError("metaapi_temporarily_unavailable", retryable=True)
        return self.margin


def sizing(*, entry: str = "4000", stop: str = "3990", tps: int = 3):
    return Day24RiskSizer.size(
        balance="1000",
        risk_percent="1",
        signal_entry_price=entry,
        signal_stop_loss=stop,
        tick_size="0.01",
        tick_value="1",
        take_profit_count=tps,
        volume_rules=BrokerVolumeRules.from_values(
            minimum="0.01", maximum="100", step="0.01"
        ),
    )


def state(
    *,
    bid: float = 3999.8,
    ask: float = 4000.0,
    free_margin: float = 1000.0,
    trade_allowed: bool = True,
    execution_ready: bool = True,
    block_reason: str | None = None,
) -> Day23LiveState:
    now = datetime(2026, 8, 11, 10, 0, tzinfo=UTC)
    price = Day23PriceState(
        symbol="XAUUSD",
        bid=bid,
        ask=ask,
        buy_price=ask,
        sell_price=bid,
        quote_time=now,
        quote_age_seconds=0.5,
        available=execution_ready,
        stale=block_reason == "price_stale",
        execution_ready=execution_ready,
        block_reason=block_reason,
    )
    return Day23LiveState(
        local_account_id=uuid4(),
        metaapi_account_id="metaapi-account",
        login_masked="****1913",
        server="VantageMarkets-Demo",
        region="london",
        read_at=now,
        account=Day23AccountState(
            currency="USD",
            balance=1000,
            equity=1000,
            margin=0,
            free_margin=free_margin,
            margin_level=None,
            leverage=500,
            trade_allowed=trade_allowed,
        ),
        price=price,
        positions=(),
        execution_ready=execution_ready,
        execution_block_reason=block_reason,
    )


def run_preflight(*, live_state: Day23LiveState, side: str, gateway: FakeMarginGateway):
    service = Day25TradePreflightService(margin_gateway=gateway)  # type: ignore[arg-type]
    stop = "3990" if side.upper() == "BUY" else "4010"
    return asyncio.run(
        service.evaluate(
            live_state=live_state,
            side=side,
            sizing=sizing(stop=stop),
            token="test-token",
        )
    )


def test_buy_at_signal_entry_proceeds_once_and_checks_whole_signal_margin_once() -> None:
    gateway = FakeMarginGateway(margin=250)
    result = run_preflight(live_state=state(ask=4000.0), side="BUY", gateway=gateway)

    assert result.proceed is True
    assert result.entry_available is True
    assert result.price_check_count == 1
    assert result.margin_check_count == 1
    assert result.position_count == 3
    assert result.position_volume == Decimal("0.01")
    assert result.total_volume == Decimal("0.03")
    assert result.positions_allowed == 3
    assert result.all_or_nothing is True
    assert result.trade_action_created is False
    assert len(gateway.calls) == 1
    assert gateway.calls[0]["volume"] == 0.03
    assert gateway.calls[0]["open_price"] == 4000.0


def test_buy_at_better_price_proceeds_without_chasing() -> None:
    gateway = FakeMarginGateway(margin=100)
    result = run_preflight(live_state=state(ask=3999.5), side="BUY", gateway=gateway)

    assert result.proceed is True
    assert result.executable_price == Decimal("3999.5")
    assert result.entry_available is True
    assert len(gateway.calls) == 1


def test_sell_at_better_price_proceeds_using_bid() -> None:
    gateway = FakeMarginGateway(margin=100)
    result = run_preflight(
        live_state=state(bid=4000.5, ask=4000.7), side="SELL", gateway=gateway
    )

    assert result.proceed is True
    assert result.executable_price == Decimal("4000.5")
    assert gateway.calls[0]["open_price"] == 4000.5


def test_buy_after_entry_has_been_missed_skips_once_without_margin_call() -> None:
    gateway = FakeMarginGateway(margin=100)
    result = run_preflight(live_state=state(ask=4000.01), side="BUY", gateway=gateway)

    assert result.proceed is False
    assert result.block_reason == "entry_price_unavailable"
    assert result.price_check_count == 1
    assert result.margin_check_count == 0
    assert result.positions_allowed == 0
    assert gateway.calls == []


def test_sell_after_entry_has_been_missed_skips_once_without_margin_call() -> None:
    gateway = FakeMarginGateway(margin=100)
    result = run_preflight(
        live_state=state(bid=3999.99, ask=4000.2), side="SELL", gateway=gateway
    )

    assert result.proceed is False
    assert result.block_reason == "entry_price_unavailable"
    assert result.positions_allowed == 0
    assert gateway.calls == []


def test_buy_price_at_or_beyond_signal_stop_is_not_a_valid_better_entry() -> None:
    gateway = FakeMarginGateway(margin=100)
    result = run_preflight(live_state=state(ask=3990.0), side="BUY", gateway=gateway)

    assert result.proceed is False
    assert result.block_reason == "entry_price_unavailable"
    assert result.positions_allowed == 0
    assert gateway.calls == []


def test_sell_price_at_or_beyond_signal_stop_is_not_a_valid_better_entry() -> None:
    gateway = FakeMarginGateway(margin=100)
    result = run_preflight(
        live_state=state(bid=4010.0, ask=4010.2), side="SELL", gateway=gateway
    )

    assert result.proceed is False
    assert result.block_reason == "entry_price_unavailable"
    assert result.positions_allowed == 0
    assert gateway.calls == []


def test_stale_price_blocks_before_margin_check() -> None:
    gateway = FakeMarginGateway(margin=100)
    result = run_preflight(
        live_state=state(
            execution_ready=False,
            block_reason="price_stale",
        ),
        side="BUY",
        gateway=gateway,
    )

    assert result.proceed is False
    assert result.block_reason == "price_stale"
    assert result.price_check_count == 1
    assert result.margin_check_count == 0
    assert gateway.calls == []


def test_insufficient_funds_blocks_entire_tp_set_not_partial_signal() -> None:
    gateway = FakeMarginGateway(margin=250.01)
    result = run_preflight(
        live_state=state(ask=4000.0, free_margin=250.0), side="BUY", gateway=gateway
    )

    assert result.proceed is False
    assert result.block_reason == "insufficient_funds"
    assert result.required_margin == Decimal("250.01")
    assert result.free_margin == Decimal("250.0")
    assert result.position_count == 3
    assert result.positions_allowed == 0
    assert result.all_or_nothing is True
    assert len(gateway.calls) == 1


def test_exactly_enough_free_margin_allows_complete_signal() -> None:
    gateway = FakeMarginGateway(margin=250.0)
    result = run_preflight(
        live_state=state(ask=4000.0, free_margin=250.0), side="BUY", gateway=gateway
    )

    assert result.proceed is True
    assert result.positions_allowed == 3
    assert result.required_margin == result.free_margin == Decimal("250.0")


def test_terminal_trading_disabled_blocks_before_margin_call() -> None:
    gateway = FakeMarginGateway(margin=100)
    result = run_preflight(
        live_state=state(ask=4000.0, trade_allowed=False), side="BUY", gateway=gateway
    )

    assert result.proceed is False
    assert result.block_reason == "trading_not_allowed"
    assert result.positions_allowed == 0
    assert gateway.calls == []


def test_margin_service_failure_blocks_signal_without_retry_or_trade() -> None:
    gateway = FakeMarginGateway(error=True)
    result = run_preflight(live_state=state(ask=4000.0), side="BUY", gateway=gateway)

    assert result.proceed is False
    assert result.block_reason == "margin_check_unavailable"
    assert result.margin_check_count == 1
    assert result.positions_allowed == 0
    assert result.trade_action_created is False
    assert len(gateway.calls) == 1


class CaptureMarginGateway(MetaApiMarginGateway):
    def __init__(self) -> None:
        super().__init__()
        self.captured: dict[str, object] | None = None

    async def _request(
        self,
        method: str,
        url: str,
        *,
        token: str,
        json_body: dict[str, object],
    ) -> httpx.Response:
        self.captured = {
            "method": method,
            "url": url,
            "token": token,
            "json": json_body,
        }
        return httpx.Response(200, json={"margin": 321.5})


def test_margin_gateway_sends_one_non_trading_aggregate_margin_request() -> None:
    gateway = CaptureMarginGateway()
    margin = asyncio.run(
        gateway.calculate_margin(
            token="test-token",
            account_id="account-1",
            region="london",
            symbol="XAUUSD",
            side="BUY",
            volume=0.03,
            open_price=4000.0,
        )
    )

    assert margin == 321.5
    assert gateway.captured is not None
    assert gateway.captured["method"] == "POST"
    assert str(gateway.captured["url"]).endswith(
        "/users/current/accounts/account-1/calculate-margin"
    )
    assert gateway.captured["json"] == {
        "symbol": "XAUUSD",
        "type": "ORDER_TYPE_BUY",
        "volume": 0.03,
        "openPrice": 4000.0,
    }


def test_margin_gateway_exposes_no_trade_or_order_placement_method() -> None:
    public_names = {
        name for name in dir(MetaApiMarginGateway) if not name.startswith("_")
    }
    assert public_names == {"calculate_margin"}
