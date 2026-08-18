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
        local_account_id=UUID(int=99), metaapi_account_id="metaapi-account", login_masked="****1913",
        server="VantageMarkets-Demo", region="london", read_at=now,
        account=Day23AccountState(currency="USD", balance=1000.0, equity=1000.0, margin=0.0,
            free_margin=1000.0, margin_level=None, leverage=500.0, trade_allowed=True),
        price=Day23PriceState(symbol="XAUUSD", bid=bid, ask=ask, buy_price=ask, sell_price=bid,
            quote_time=now, quote_age_seconds=0.1, available=True, stale=False,
            execution_ready=True, block_reason=None, profit_tick_value=1.0, loss_tick_value=1.0),
        positions=(), execution_ready=True, execution_block_reason=None,
    )


class _FakeDay23:
    states: list[Day23LiveState] = [_live_state()]
    reads = 0
    def __init__(self, **_: object) -> None: pass
    @staticmethod
    def executable_price(state: Day23LiveState, side: str) -> float:
        normalized = side.strip().upper()
        if normalized == "BUY": return float(state.price.ask)
        if normalized == "SELL": return float(state.price.bid)
        raise Day26ExecutionError("trade_side_invalid")
    async def read_owner_live_state(self, owner_user_id: UUID) -> Day23LiveState:
        assert owner_user_id == OWNER
        index = min(self.__class__.reads, len(self.__class__.states) - 1)
        self.__class__.reads += 1
        return self.__class__.states[index]


class _ReadGateway:
    def __init__(self) -> None: self.position_reads = 0
    async def read_symbol_specification(self, **_: object) -> dict[str, object]:
        return {"tickSize": 0.01, "minVolume": 0.01, "maxVolume": 100.0, "volumeStep": 0.01}
    async def read_positions(self, **_: object) -> list[dict[str, object]]:
        self.position_reads += 1; return []


class _MarginGateway:
    def __init__(self) -> None: self.calls: list[dict[str, object]] = []
    async def calculate_margin(self, **kwargs: object) -> float:
        self.calls.append(kwargs); return 50.0


class _TradeGateway:
    def __init__(self) -> None: self.calls: list[dict[str, object]] = []
    async def place_market_order(self, **kwargs: object) -> MetaApiMarketOrderResult:
        self.calls.append(kwargs)
        return MetaApiMarketOrderResult(order_id=f"order-{len(self.calls)}", position_id=None,
            numeric_code=10009, string_code="TRADE_RETCODE_DONE")


class _Harness(Day26Mt5ExecutionService):
    def __init__(self, *, entry_low: str="4000", entry_high: str="4000", side: str="BUY",
                 runner: bool=False, zone_wait_seconds: float=300.0, zone_poll_seconds: float=0.01,
                 entry_tolerance: str|None=None) -> None:
        self.read, self.margin, self.trade = _ReadGateway(), _MarginGateway(), _TradeGateway()
        super().__init__(session_factory=None, cipher=None, read_gateway=self.read, margin_gateway=self.margin,
            trade_gateway=self.trade, zone_wait_seconds=zone_wait_seconds, zone_poll_seconds=zone_poll_seconds,
            entry_tolerance=entry_tolerance)  # type: ignore[arg-type]
        low, high = Decimal(entry_low), Decimal(entry_high)
        zone_buffer = Decimal("1") if low != high else Decimal("10")
        if side == "BUY": stop, tps = low-zone_buffer, (high+10, high+20, high+30)
        else: stop, tps = high+zone_buffer, (low-10, low-20, low-30)
        self.signal = _SignalInput(signal_id=SIGNAL, symbol="XAUUSD", side=side, entry_low=low, entry_high=high,
            stop_loss=stop, take_profits=tps, has_open_runner=runner, signal_requests_double_lot=False,
            source_revision_index=0, source_posted_at=datetime.now(UTC))
        self.order_ids: list[str] = []
    def _load_inputs(self, owner_user_id: UUID, signal_id: UUID):  # type: ignore[override]
        return self.signal, _AccountInput(UUID(int=100), "metaapi-account", b"cipher")
    def _decrypt_token(self, account: _AccountInput) -> str: return "test-token-with-terminal-access"  # type: ignore[override]
    def _assert_signal_still_current(self, owner_user_id: UUID, signal: _SignalInput) -> None: return None  # type: ignore[override]
    def _create_planned_positions(self, *, signal, **_: object):  # type: ignore[override]
        targets: list[Decimal|None] = list(signal.take_profits) + ([None] if signal.has_open_runner else [])
        return tuple(_PlannedPosition(UUID(int=i), i, tp, f"SS_{i:012d}_{i}") for i,tp in enumerate(targets,1))
    def _record_order_id(self, local_position_id: UUID, order_id: str) -> None: self.order_ids.append(order_id)
    def _map_broker_positions(self, *, sizing, execution_entry, planned, order_ids, **_):  # type: ignore[override]
        return tuple(Day26MappedPosition(item.local_position_id,item.tp_index,item.take_profit,sizing.volume,
            item.client_id,order_ids[item.client_id],f"position-{item.tp_index}",execution_entry) for item in planned)
    def _audit(self, **_: object) -> None: return None  # type: ignore[override]


def _patch_states(monkeypatch: pytest.MonkeyPatch, *states: Day23LiveState) -> None:
    monkeypatch.setattr(day26_module, "Day23Mt5ReadService", _FakeDay23)
    _FakeDay23.states, _FakeDay23.reads = list(states), 0


def test_exact_three_tps_submit_three_orders_without_margin_veto(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_states(monkeypatch, _live_state(bid=3999.8, ask=4000.0)); service=_Harness()
    result=asyncio.run(service.execute_owner_demo_signal(owner_user_id=OWNER, signal_id=SIGNAL, risk_percent="1", double_lot_approved=False))
    assert result.signal_entry_price==Decimal("4000") and len(service.trade.calls)==3
    assert [c["take_profit"] for c in service.trade.calls]==[4010.0,4020.0,4030.0]
    assert service.margin.calls==[]


def test_exact_mismatch_beyond_tolerance_preserves_day25(monkeypatch: pytest.MonkeyPatch) -> None:
    # Well outside the entry tolerance the provider's price is simply not available,
    # and the trade is skipped. No chase, no wait, no substitution.
    _patch_states(monkeypatch, _live_state(bid=4004.9, ask=4005.0)); service=_Harness()
    with pytest.raises(Day26ExecutionError, match="entry_price_unavailable"):
        asyncio.run(service.execute_owner_demo_signal(owner_user_id=OWNER, signal_id=SIGNAL, risk_percent="1", double_lot_approved=False))
    assert service.trade.calls==[] and service.margin.calls==[]


def test_exact_entry_within_tolerance_fills_and_sizes_off_actual_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single-price provider entry is tradeable when the market is close enough.

    Gold ticks in cents, so requiring the live price to equal the provider's round
    number exactly made these signals effectively unexecutable. The fill is sized
    off the real executable price rather than the provider's number, so the
    configured risk percentage stays honest.
    """
    _patch_states(monkeypatch, _live_state(bid=3999.7, ask=3999.8)); service=_Harness()
    result=asyncio.run(service.execute_owner_demo_signal(owner_user_id=OWNER, signal_id=SIGNAL, risk_percent="1", double_lot_approved=False))
    # A BUY fills at the ask, and that actual price becomes the sizing basis rather
    # than the provider's 4000, so the stop distance used for sizing is the real one.
    assert result.signal_entry_price==Decimal("3999.8")
    assert len(service.trade.calls)==3 and service.margin.calls==[]


def test_zero_tolerance_restores_strict_equality(monkeypatch: pytest.MonkeyPatch) -> None:
    # The tolerance is configurable and can be switched off entirely.
    _patch_states(monkeypatch, _live_state(bid=4000.1, ask=4000.2))
    service=_Harness(entry_tolerance="0")
    with pytest.raises(Day26ExecutionError, match="entry_price_unavailable"):
        asyncio.run(service.execute_owner_demo_signal(owner_user_id=OWNER, signal_id=SIGNAL, risk_percent="1", double_lot_approved=False))
    assert service.trade.calls==[] and service.margin.calls==[]


def test_buy_zone_uses_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_states(monkeypatch, _live_state(bid=4391.8, ask=4392.0)); service=_Harness(entry_low="4391", entry_high="4394")
    result=asyncio.run(service.execute_owner_demo_signal(owner_user_id=OWNER, signal_id=SIGNAL, risk_percent="1", double_lot_approved=False))
    assert result.signal_entry_price==Decimal("4392.0") and len(service.trade.calls)==3


def test_sell_zone_uses_bid(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_states(monkeypatch, _live_state(bid=4392.0, ask=4394.5)); service=_Harness(entry_low="4391", entry_high="4394", side="SELL")
    result=asyncio.run(service.execute_owner_demo_signal(owner_user_id=OWNER, signal_id=SIGNAL, risk_percent="1", double_lot_approved=False))
    assert result.signal_entry_price==Decimal("4392.0")


def test_zone_waits_for_touch(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_states(monkeypatch, _live_state(bid=4396,ask=4396.2), _live_state(bid=4392.8,ask=4393))
    service=_Harness(entry_low="4391",entry_high="4394",zone_wait_seconds=1,zone_poll_seconds=.01)
    result=asyncio.run(service.execute_owner_demo_signal(owner_user_id=OWNER, signal_id=SIGNAL, risk_percent="1", double_lot_approved=False))
    assert result.signal_entry_price==Decimal("4393.0") and _FakeDay23.reads>=2


def test_zone_expiry_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_states(monkeypatch, _live_state(bid=4396,ask=4396.2)); service=_Harness(entry_low="4391",entry_high="4394",zone_wait_seconds=0)
    with pytest.raises(Day26ExecutionError, match="zone_not_reached"):
        asyncio.run(service.execute_owner_demo_signal(owner_user_id=OWNER, signal_id=SIGNAL, risk_percent="1", double_lot_approved=False))
    assert service.trade.calls==[] and service.margin.calls==[]


def test_runner_adds_fourth_position_without_margin_veto(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_states(monkeypatch, _live_state(bid=3999.8,ask=4000)); service=_Harness(runner=True)
    result=asyncio.run(service.execute_owner_demo_signal(owner_user_id=OWNER, signal_id=SIGNAL, risk_percent="1", double_lot_approved=False))
    assert len(result.positions)==4 and service.trade.calls[-1]["take_profit"] is None
    assert service.margin.calls==[]


class _CaptureTradeGateway(MetaApiTradeGateway):
    def __init__(self)->None: super().__init__(); self.payload=None
    async def _request(self, method:str, url:str, *, token:str, json_body:dict[str,object]):
        self.payload=json_body; return httpx.Response(200,json={"numericCode":10009,"stringCode":"TRADE_RETCODE_DONE","orderId":"12345"})


def test_runner_gateway_omits_take_profit() -> None:
    gateway=_CaptureTradeGateway()
    asyncio.run(gateway.place_market_order(token="test-token",account_id="account-1",region="london",side="BUY",symbol="XAUUSD",volume=.01,stop_loss=3990,take_profit=None,client_id="SS_000000000004_4"))
    assert gateway.payload is not None and "takeProfit" not in gateway.payload and "takeProfitUnits" not in gateway.payload


def test_symbol_specification_is_read_before_waiting_for_the_zone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Static preflight data must not sit in the critical path after price is tradeable.

    A live TDC zone signal was lost because the engine waited for price to enter the
    provider's zone and only then fetched the symbol specification and calculated
    margin. By the time it submitted, gold had ticked back out of the zone and every
    leg was refused. The specification is static for the symbol, so it belongs before
    the wait, not between the zone opening and the order.
    """
    reads_when_spec_fetched: list[int] = []

    class _OrderedRead(_ReadGateway):
        async def read_symbol_specification(self, **kwargs: object):
            reads_when_spec_fetched.append(_FakeDay23.reads)
            return await super().read_symbol_specification(**kwargs)

    # First quote sits below the zone so the wait loop must run; the second is inside.
    _patch_states(
        monkeypatch,
        _live_state(bid=4380.0, ask=4380.2),
        _live_state(bid=4392.0, ask=4392.2),
    )
    service = _Harness(entry_low="4391", entry_high="4394", zone_poll_seconds=0.01)
    service._read_gateway = _OrderedRead()

    asyncio.run(
        service.execute_owner_demo_signal(
            owner_user_id=OWNER, signal_id=SIGNAL, risk_percent="1", double_lot_approved=False
        )
    )

    # Exactly one live-state read (the pre-wait one) had happened when the
    # specification was fetched. If it were fetched after the wait, the loop's
    # additional reads would already be counted here.
    assert reads_when_spec_fetched == [1]
