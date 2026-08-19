from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.critical_entry_policy import CriticalEntry
from app.metaapi_trade_gateway import MetaApiTradeGateway
from app.paper_pending_gateway import PaperPendingOrderGateway, PaperPendingOrderRequest
from app.trading_execution_canonical import CanonicalTradingExecutionService


def _entry(index: int, order_type: str, price: str) -> CriticalEntry:
    return CriticalEntry(entry_index=index, order_type=order_type, price=Decimal(price))


def test_tig_two_entries_four_targets_use_four_atomic_positions_not_eight() -> None:
    entries = (
        _entry(1, "market", "4413"),
        _entry(2, "buy_limit", "4409"),
    )
    targets = (Decimal("4418"), Decimal("4424"), Decimal("4430"), None)
    plan = CanonicalTradingExecutionService._allocation_pairs(entries, targets)
    assert len(plan) == 4
    assert {item.entry.entry_index for item in plan} == {1, 2}
    assert {item.tp_index for item in plan} == {1, 2, 3, 4}
    runner = next(item for item in plan if item.take_profit is None)
    assert runner.entry.entry_index == 2


def test_tdc_six_layers_four_targets_use_six_atomic_positions_not_twenty_four() -> None:
    entries = tuple(
        _entry(index, "buy_limit", str(4385 - index))
        for index in range(1, 7)
    )
    targets = (Decimal("4386"), Decimal("4389"), Decimal("4393"), None)
    plan = CanonicalTradingExecutionService._allocation_pairs(entries, targets)
    assert len(plan) == 6
    assert {item.entry.entry_index for item in plan} == {1, 2, 3, 4, 5, 6}
    assert {item.tp_index for item in plan} == {1, 2, 3, 4}
    assert sum(1 for item in plan if item.take_profit is None) == 1
    runner = next(item for item in plan if item.take_profit is None)
    assert runner.entry.entry_index == 6


class _FakeTransport(MetaApiTradeGateway):
    def __init__(self) -> None:
        pass

    async def _trade_request(self, **kwargs):
        self.last = kwargs
        return {
            "orderId": "12345",
            "positionId": "",
            "numericCode": 10008,
            "stringCode": "TRADE_RETCODE_PLACED",
        }


@pytest.mark.asyncio
async def test_pending_gateway_resolves_narrow_wrapper_to_real_transport() -> None:
    transport = _FakeTransport()
    wrapped = SimpleNamespace(_base=transport)
    gateway = PaperPendingOrderGateway(wrapped)
    result = await gateway.place_pending_order(
        account_environment="demo",
        token="token",
        account_id="account",
        region="london",
        request=PaperPendingOrderRequest(
            order_type="buy_limit",
            symbol="XAUUSD",
            volume=0.01,
            open_price=4384,
            stop_loss=4378,
            take_profit=4386,
            client_id="SS_ABCDEFGHIJKL_E1T1",
        ),
    )
    assert result.order_id == "12345"
    assert transport.last["json_body"]["actionType"] == "ORDER_TYPE_BUY_LIMIT"
    assert transport.last["json_body"]["openPrice"] == 4384.0
