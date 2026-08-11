import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import httpx
import pytest

import app.mt5_execution_day26 as day26_module
from app.metaapi_trade_gateway import MetaApiMarketOrderResult, MetaApiTradeGateway
from app.mt5_execution_day26 import (
    Day26ExecutionError,
    Day26MappedPosition,
    Day26Mt5ExecutionService,
    _AccountInput,
    _PlannedPosition,
    _SignalInput,
)
from app.mt5_read_service_day23 import Day23AccountState, Day23LiveState, Day23PriceState

OWNER = UUID("ea604df2-f8ee-47d1-bc51-f0078dbf160d")
SIGNAL = UUID("3e7ec830-9a9a-42df-b908-b331863ff6a4")


def _live_state(*, bid: float = 4000.0, ask: float = 4000.0) -> Day23LiveState:
    now = datetime.now(UTC)
    return Day23LiveState(
        local_account_id=UUID(int=99),
        metaapi_account_id="metaapi-account",
        login_masked="****1913",
        server="VantageMarkets-Demo",
        region="london",
        read_at=now,
        account=Day23AccountState(
            currency="USD",
            balance=1000.0,
            equity=1000.0,
            margin=0.0,
            free_margin=1000.0,
            margin_level=None,
            leverage=500.0,
            trade_allowed=True,
        ),
        price=Day23PriceState(
            symbol="XAUUSD",
            bid=bid,
            ask=ask,
            buy_price=ask,
            sell_price=bid,
            quote_time=now,
            quote_age_seconds=0.1,
            available=True,
            stale=False,
            execution_ready=True,
            block_reason=None,
            profit_tick_value=1.0,
            loss_tick_value=1.0,
        ),
        positions=(),
        execution_ready=True,
        execution_block_reason=None,
    )


class _FakeDay23:
    states: list[Day23LiveState] = [_live_state()]
    reads = 0

    def __init__(self, **_: object) -> None:
        pass

    async def read_owner_live_state(self, owner_user_id: UUID) -> Day23LiveState:
        assert owner_user_id == OWNER
        index = min(self.__class__.reads, len(self.__class__.states) - 1)
        self.__class__.reads += 1
        return self.__class__.states[index]


class _ReadGateway:
    def __init__(self) -> None:
        self.position_reads = 0

    async def read_symbol_specification(self, **_: object) -> dict[str, object]:
        return {
            "tickSize": 0.01,
            "minVolume": 0.01,
            "maxVolume": 100.0,
            "volumeStep": 0.01,
        }

    async def read_positions(self, **_: object) -> list[dict[str, object]]:
        self.position_reads += 1
        return []


class _MarginGateway:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def calculate_margin(self, **kwargs: object) -> float:
        self.calls.append(kwargs)
        return 50.0


class _TradeGateway:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def place_market_order(self, **kwargs: object) -> MetaApiMarketOrderResult:
        self.calls.append(kwargs)
        return MetaApiMarketOrderResult(
            order_id=f"order-{len(self.calls)}",
            position_id=None,
            numeric_code=10009,
            string_code="TRADE_RETCODE_DONE",
        )


class _Harness(Day26Mt5ExecutionService):
    def __init__(
        self,
        *,
        entry_low: str = "4000",
        entry_high: str = "4000",
        side: str = "BUY",
        runner: bool = False,
        double: bool = False,
        zone_wait_seconds: float = 300.0,
        zone_poll_seconds: float = 0.01,
    ) -> None:
        self.read = _ReadGateway()
        self.margin = _MarginGateway()
        self.trade = _TradeGateway()
        super().__init__(
            session_factory=None,  # type: ignore[arg-type]
            cipher=None,  # type: ignore[arg-type]
            read_gateway=self.read,  # type: ignore[arg-type]
            margin_gateway=self.margin,  # type: ignore[arg-type]
            trade_gateway=self.trade,  # type: ignore[arg-type]
            zone_wait_seconds=zone_wait_seconds,
            zone_poll_seconds=zone_poll_seconds,
        )
        if side == "BUY":
            stop = Decimal(entry_low) - Decimal("10")
            tps = (
                Decimal(entry_high) + Decimal("10"),
                Decimal(entry_high) + Decimal("20"),
                Decimal(entry_high) + Decimal("30"),
            )
        else:
            stop = Decimal(entry_high) + Decimal("10")
            tps = (
                Decimal(entry_low) - Decimal("10"),
                Decimal(entry_low) - Decimal("20"),
                Decimal(entry_low) - Decimal("30"),
            )
        self.signal = _SignalInput(
            signal_id=SIGNAL,
            symbol="XAUUSD",
            side=side,
            entry_low=Decimal(entry_low),
            entry_high=Decimal(entry_high),
            stop_loss=stop,
            take_profits=tps,
            has_open_runner=runner,
            signal_requests_double_lot=double,
            source_revision_index=0,
            source_posted_at=datetime.now(UTC),
        )
        self.order_ids: list[str] = []
        self.audits: list[tuple[str, dict[str, object]]] = []

    def _load_inputs(self, owner_user_id: UUID, signal_id: UUID):  # type: ignore[override]
        assert owner_user_id == OWNER and signal_id == SIGNAL
        return self.signal, _AccountInput(UUID(int=100), "metaapi-account", b"cipher")

    def _decrypt_token(self, account: _AccountInput) -> str:  # type: ignore[override]
        return "test-token-with-terminal-access"

    def _assert_signal_still_current(self, owner_user_id: UUID, signal: _SignalInput) -> None:  # type: ignore[override]
        assert owner_user_id == OWNER and signal.signal_id == SIGNAL

    def _create_planned_positions(self, *, signal, **_: object):  # type: ignore[override]
        targets: list[Decimal | None] = list(signal.take_profits)
        if signal.has_open_runner:
            targets.append(None)
        return tuple(
            _PlannedPosition(
                local_position_id=UUID(int=index),
                tp_index=index,
                take_profit=tp,
                client_id=f"SS_{index:012d}_{index}",
            )
            for index, tp in enumerate(targets, start=1)
        )

    def _record_order_id(self, local_position_id: UUID, order_id: str) -> None:
        self.order_ids.append(order_id)

    def _map_broker_positions(
        self,
        *,
        signal,
        sizing,
        execution_entry,
        planned,
        order_ids,
        **_,
    ):  # type: ignore[override]
        return tuple(
            Day26MappedPosition(
                local_position_id=item.local_position_id,
                tp_index=item.tp_index,
                take_profit=item.take_profit,
                volume=sizing.volume,
                client_id=item.client_id,
                broker_order_id=order_ids[item.client_id],
                broker_position_id=f"position-{item.tp_index}",
                broker_open_price=execution_entry,
            )
            for item in planned
        )

    def _audit(self, *, event_type, payload, **_):  # type: ignore[override]
        self.audits.append((event_type, payload))


def _patch_states(monkeypatch: pytest.MonkeyPatch, *states: Day23LiveState) -> None:
    monkeypatch.setattr(day26_module, "Day23Mt5ReadService", _FakeDay23)
    _FakeDay23.states = list(states)
    _FakeDay23.reads = 0


def test_exact_three_tps_submit_three_orders_and_one_margin_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_states(monkeypatch, _live_state(bid=3999.8, ask=4000.0))
    service = _Harness()

    result = asyncio.run(
        service.execute_owner_demo_signal(
            owner_user_id=OWNER,
            signal_id=SIGNAL,
            risk_percent="1",
            double_lot_approved=False,
        )
    )

    assert result.signal_entry_price == Decimal("4000")
    assert len(result.positions) == 3
    assert len(service.trade.calls) == 3
    assert [row["take_profit"] for row in service.trade.calls] == [4010.0, 4020.0, 4030.0]
    assert {row["stop_loss"] for row in service.trade.calls} == {3990.0}
    assert {row["volume"] for row in service.trade.calls} == {0.01}
    assert len({row["client_id"] for row in service.trade.calls}) == 3
    assert len(service.order_ids) == 3
    assert len(service.margin.calls) == 1
    assert service.margin.calls[0]["volume"] == 0.03
    assert service.read.position_reads == 1


def test_exact_entry_mismatch_preserves_day25_and_places_no_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_states(monkeypatch, _live_state(bid=4000.1, ask=4000.2))
    service = _Harness()

    with pytest.raises(Day26ExecutionError, match="entry_price_unavailable"):
        asyncio.run(
            service.execute_owner_demo_signal(
                owner_user_id=OWNER,
                signal_id=SIGNAL,
                risk_percent="1",
                double_lot_approved=False,
            )
        )

    assert service.trade.calls == []
    assert service.margin.calls == []


def test_buy_zone_executes_immediately_at_live_ask_inside_zone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_states(monkeypatch, _live_state(bid=4391.8, ask=4392.0))
    service = _Harness(entry_low="4389", entry_high="4394")

    result = asyncio.run(
        service.execute_owner_demo_signal(
            owner_user_id=OWNER,
            signal_id=SIGNAL,
            risk_percent="1",
            double_lot_approved=False,
        )
    )

    assert result.signal_entry_price == Decimal("4392.0")
    assert len(service.trade.calls) == 3
    assert len(service.margin.calls) == 1
    assert service.margin.calls[0]["open_price"] == 4392.0
    success = next(payload for event, payload in service.audits if event == "mt5.day26_execution_success")
    assert success["entry_is_zone"] is True
    assert success["execution_entry"] == "4392.0"


def test_sell_zone_uses_bid_not_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    # Bid is inside the zone while ask is above it; SELL must still execute.
    _patch_states(monkeypatch, _live_state(bid=4392.0, ask=4394.5))
    service = _Harness(entry_low="4389", entry_high="4394", side="SELL")

    result = asyncio.run(
        service.execute_owner_demo_signal(
            owner_user_id=OWNER,
            signal_id=SIGNAL,
            risk_percent="1",
            double_lot_approved=False,
        )
    )

    assert result.signal_entry_price == Decimal("4392.0")
    assert len(service.trade.calls) == 3


def test_zone_waits_for_first_valid_touch_then_executes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_states(
        monkeypatch,
        _live_state(bid=4396.0, ask=4396.2),
        _live_state(bid=4392.8, ask=4393.0),
    )
    service = _Harness(
        entry_low="4389",
        entry_high="4394",
        zone_wait_seconds=1.0,
        zone_poll_seconds=0.01,
    )

    result = asyncio.run(
        service.execute_owner_demo_signal(
            owner_user_id=OWNER,
            signal_id=SIGNAL,
            risk_percent="1",
            double_lot_approved=False,
        )
    )

    assert result.signal_entry_price == Decimal("4393.0")
    assert _FakeDay23.reads >= 2
    assert len(service.trade.calls) == 3


def test_zone_outside_with_expired_window_skips_without_margin_or_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_states(monkeypatch, _live_state(bid=4396.0, ask=4396.2))
    service = _Harness(
        entry_low="4389",
        entry_high="4394",
        zone_wait_seconds=0,
    )

    with pytest.raises(Day26ExecutionError, match="zone_not_reached"):
        asyncio.run(
            service.execute_owner_demo_signal(
                owner_user_id=OWNER,
                signal_id=SIGNAL,
                risk_percent="1",
                double_lot_approved=False,
            )
        )

    assert service.trade.calls == []
    assert service.margin.calls == []


def test_tp_open_adds_runner_without_take_profit_and_funds_whole_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_states(monkeypatch, _live_state(bid=3999.8, ask=4000.0))
    service = _Harness(runner=True)

    result = asyncio.run(
        service.execute_owner_demo_signal(
            owner_user_id=OWNER,
            signal_id=SIGNAL,
            risk_percent="1",
            double_lot_approved=False,
        )
    )

    assert len(result.positions) == 4
    assert len(service.trade.calls) == 4
    assert service.trade.calls[-1]["take_profit"] is None
    assert result.positions[-1].take_profit is None
    assert len(service.margin.calls) == 1
    assert service.margin.calls[0]["volume"] == 0.04


def test_double_signal_respects_user_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_states(monkeypatch, _live_state(bid=3999.8, ask=4000.0))
    off = _Harness(double=True)
    off_result = asyncio.run(
        off.execute_owner_demo_signal(
            owner_user_id=OWNER,
            signal_id=SIGNAL,
            risk_percent="1",
            double_lot_approved=False,
        )
    )
    assert off_result.effective_risk_percent == Decimal("1")
    assert off_result.double_lot_applied is False

    _patch_states(monkeypatch, _live_state(bid=3999.8, ask=4000.0))
    on = _Harness(double=True)
    on_result = asyncio.run(
        on.execute_owner_demo_signal(
            owner_user_id=OWNER,
            signal_id=SIGNAL,
            risk_percent="1",
            double_lot_approved=True,
        )
    )
    assert on_result.effective_risk_percent == Decimal("2")
    assert on_result.double_lot_applied is True
    assert {row["volume"] for row in on.trade.calls} == {0.02}


class _CaptureTradeGateway(MetaApiTradeGateway):
    def __init__(self) -> None:
        super().__init__()
        self.payload: dict[str, object] | None = None

    async def _request(self, method: str, url: str, *, token: str, json_body: dict[str, object]):
        self.payload = {"method": method, "url": url, "json": json_body}
        return httpx.Response(
            200,
            json={
                "numericCode": 10009,
                "stringCode": "TRADE_RETCODE_DONE",
                "orderId": "12345",
            },
        )


def test_trade_gateway_sends_provider_sl_tp_and_client_id() -> None:
    gateway = _CaptureTradeGateway()
    result = asyncio.run(
        gateway.place_market_order(
            token="test-token",
            account_id="account-1",
            region="london",
            side="SELL",
            symbol="XAUUSD",
            volume=0.01,
            stop_loss=4070.0,
            take_profit=4043.0,
            client_id="SS_000000000001_1",
        )
    )
    assert result.order_id == "12345"
    assert gateway.payload is not None
    assert gateway.payload["json"] == {
        "actionType": "ORDER_TYPE_SELL",
        "symbol": "XAUUSD",
        "volume": 0.01,
        "stopLoss": 4070.0,
        "stopLossUnits": "ABSOLUTE_PRICE",
        "clientId": "SS_000000000001_1",
        "takeProfit": 4043.0,
        "takeProfitUnits": "ABSOLUTE_PRICE",
    }


def test_trade_gateway_runner_omits_take_profit_fields() -> None:
    gateway = _CaptureTradeGateway()
    asyncio.run(
        gateway.place_market_order(
            token="test-token",
            account_id="account-1",
            region="london",
            side="BUY",
            symbol="XAUUSD",
            volume=0.01,
            stop_loss=3990.0,
            take_profit=None,
            client_id="SS_000000000004_4",
        )
    )
    assert gateway.payload is not None
    assert gateway.payload["json"] == {
        "actionType": "ORDER_TYPE_BUY",
        "symbol": "XAUUSD",
        "volume": 0.01,
        "stopLoss": 3990.0,
        "stopLossUnits": "ABSOLUTE_PRICE",
        "clientId": "SS_000000000004_4",
    }


def test_trade_gateway_compensating_close_uses_position_id() -> None:
    gateway = _CaptureTradeGateway()
    asyncio.run(
        gateway.close_position(
            token="test-token",
            account_id="account-1",
            region="london",
            position_id="position-123",
        )
    )
    assert gateway.payload is not None
    assert gateway.payload["json"] == {
        "actionType": "POSITION_CLOSE_ID",
        "positionId": "position-123",
    }


def test_day26_trade_gateway_surface_is_entry_plus_compensation_only() -> None:
    public_names = {name for name in dir(MetaApiTradeGateway) if not name.startswith("_")}
    assert public_names == {"place_market_order", "close_position"}
