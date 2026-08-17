from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.critical_entry_policy import (
    augment_management_actions,
    parse_critical_entries,
)
from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_trade_gateway import MetaApiTradeGateway
from app.mt5_execution_day26 import Day26ExecutionError
from app.mt5_management_day27 import Day27ManagementError
from app.paper_critical_execution import PaperCriticalExecutionService
from app.paper_critical_management import PaperCriticalManagementService, _LayerPosition
from app.paper_partial_close_gateway import PaperPartialCloseGateway
from app.paper_pending_gateway import PaperPendingOrderGateway, PaperPendingOrderRequest
from app.paper_pending_reconciler import PaperPendingReconciler


def _position(entry: int, tp: int, price: str) -> _LayerPosition:
    return _LayerPosition(
        id=uuid4(),
        entry_index=entry,
        tp_index=tp,
        broker_position_id=f"p-{entry}-{tp}",
        broker_order_id=f"o-{entry}-{tp}",
        status="open",
        stop_loss=Decimal("4300"),
        take_profit=Decimal("4400") + Decimal(tp),
        entry_price=Decimal(price),
        volume=Decimal("0.02"),
    )


def test_sureshot_pending_order_is_exact_buy_limit() -> None:
    entries = parse_critical_entries(
        "XAUUSD BUY LIMIT 4390\nSL: 4380\nTP: 4410\n--Trade by Grace",
        side="BUY",
        entry_low="4390",
        entry_high="4390",
    )
    assert len(entries) == 1
    assert entries[0].entry_index == 1
    assert entries[0].order_type == "buy_limit"
    assert entries[0].price == Decimal("4390")


def test_tig_second_entry_becomes_retracement_limit_not_a_second_market_chase() -> None:
    entries = parse_critical_entries(
        "BUY XAUUSD\nENTRY: 4320\nSecond entry: 4315\nSL: 4303\nTP1: 4326",
        side="BUY",
        entry_low="4315",
        entry_high="4320",
    )
    assert [(item.entry_index, item.order_type, item.price) for item in entries] == [
        (1, "market", Decimal("4320")),
        (2, "buy_limit", Decimal("4315")),
    ]


def test_layer_management_keeps_second_entry_scope() -> None:
    actions = augment_management_actions(
        "Second entry is currently running with +50 pips, Book partial. Move SL to 4373",
        (
            {"type": "close", "target": "TP1", "value": None},
            {"type": "edit_stop_loss", "target": "all", "value": "4373"},
        ),
    )
    assert {"type": "close", "target": "entry_2_partial_tp1", "value": None} in actions
    assert {"type": "edit_stop_loss", "target": "entry_2", "value": "4373"} in actions


def test_leave_best_buy_closes_highest_price_layers_only() -> None:
    positions = (
        _position(1, 1, "4350"),
        _position(2, 1, "4346"),
        _position(3, 1, "4342"),
        _position(4, 1, "4338"),
    )
    selected = PaperCriticalManagementService._select_layer_positions(
        positions,
        "worst_3_layers",
        side="BUY",
    )
    assert {item.entry_index for item in selected} == {1, 2, 3}
    assert {item.entry_index for item in positions if item not in selected} == {4}


def test_leave_best_sell_closes_lowest_price_layers_only() -> None:
    positions = (
        _position(1, 1, "4350"),
        _position(2, 1, "4354"),
        _position(3, 1, "4358"),
        _position(4, 1, "4362"),
    )
    selected = PaperCriticalManagementService._select_layer_positions(
        positions,
        "worst_3_layers",
        side="SELL",
    )
    assert {item.entry_index for item in selected} == {1, 2, 3}
    assert {item.entry_index for item in positions if item not in selected} == {4}


def test_leave_best_refuses_to_close_every_available_layer() -> None:
    positions = (_position(1, 1, "4350"), _position(2, 1, "4346"))
    with pytest.raises(Day27ManagementError, match="layer_close_would_remove_best"):
        PaperCriticalManagementService._select_layer_positions(
            positions,
            "worst_2_layers",
            side="BUY",
        )


def test_partial_volume_uses_broker_step_and_leaves_valid_remainder() -> None:
    close, remain = PaperCriticalManagementService._partial_volumes(
        current=Decimal("0.03"),
        minimum=Decimal("0.01"),
        step=Decimal("0.01"),
    )
    assert close == Decimal("0.01")
    assert remain == Decimal("0.02")


def test_partial_of_single_minimum_lot_fails_closed() -> None:
    with pytest.raises(Day27ManagementError, match="partial_volume_below_broker_minimum"):
        PaperCriticalManagementService._partial_volumes(
            current=Decimal("0.01"),
            minimum=Decimal("0.01"),
            step=Decimal("0.01"),
        )


def test_layer_risk_guard_rejects_broker_minimum_overexposure() -> None:
    # Two layers are meant to share one 1% per-TP budget on a 10k account = $100.
    sizings = (
        SimpleNamespace(actual_risk_per_position=Decimal("60"), double_lot_applied=False),
        SimpleNamespace(actual_risk_per_position=Decimal("60"), double_lot_applied=False),
    )
    with pytest.raises(Day26ExecutionError, match="layer_risk_budget_exceeded_by_broker_minimum"):
        PaperCriticalExecutionService._assert_layer_risk_cap(
            real_balance=Decimal("10000"),
            risk_percent=Decimal("1"),
            double_applied=False,
            sizings=sizings,
            tp_count=3,
        )


@pytest.mark.asyncio
async def test_pending_gateway_cannot_operate_non_demo_account() -> None:
    gateway = PaperPendingOrderGateway(MetaApiTradeGateway())
    with pytest.raises(MetaApiGatewayError, match="paper_pending_demo_account_required"):
        await gateway.place_pending_order(
            account_environment="live",
            token="x" * 40,
            account_id="account",
            region="london",
            request=PaperPendingOrderRequest(
                order_type="buy_limit",
                symbol="XAUUSD",
                volume=0.01,
                open_price=4390,
                stop_loss=4380,
                take_profit=4410,
                client_id="SS_ABCDEFGHIJKL_E1T1",
            ),
        )


@pytest.mark.asyncio
async def test_partial_gateway_cannot_operate_non_demo_account() -> None:
    gateway = PaperPartialCloseGateway(MetaApiTradeGateway())
    with pytest.raises(MetaApiGatewayError, match="paper_partial_demo_account_required"):
        await gateway.close_partial(
            account_environment="live",
            token="x" * 40,
            account_id="account",
            region="london",
            position_id="123",
            volume=0.01,
        )


def test_pending_fill_mapping_requires_exact_trade_identity() -> None:
    local = {
        "symbol": "XAUUSD",
        "side": "BUY",
        "volume": Decimal("0.01"),
        "stop_loss": Decimal("4380"),
        "take_profit": Decimal("4410"),
    }
    position_id, open_price = PaperPendingReconciler._validate_fill(
        local,
        {
            "id": "777",
            "symbol": "XAUUSD",
            "type": "POSITION_TYPE_BUY",
            "volume": 0.01,
            "stopLoss": 4380,
            "takeProfit": 4410,
            "openPrice": 4390,
        },
    )
    assert position_id == "777"
    assert open_price == Decimal("4390")


def test_pending_fill_wrong_side_is_never_mapped() -> None:
    local = {
        "symbol": "XAUUSD",
        "side": "BUY",
        "volume": Decimal("0.01"),
        "stop_loss": Decimal("4380"),
        "take_profit": Decimal("4410"),
    }
    with pytest.raises(ValueError, match="pending_fill_side_mismatch"):
        PaperPendingReconciler._validate_fill(
            local,
            {
                "id": "777",
                "symbol": "XAUUSD",
                "type": "POSITION_TYPE_SELL",
                "volume": 0.01,
                "stopLoss": 4380,
                "takeProfit": 4410,
                "openPrice": 4390,
            },
        )
