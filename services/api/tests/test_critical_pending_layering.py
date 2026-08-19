from decimal import Decimal
from inspect import signature
from uuid import uuid4

import pytest

from app.critical_entry_policy import (
    augment_management_actions,
    parse_critical_entries,
)
from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_pending_gateway import MetaApiPendingOrderGateway
from app.metaapi_trade_gateway import MetaApiTradeGateway
from app.mt5_management_day27 import Day27ManagementError
from app.paper_critical_execution import PaperCriticalExecutionService
from app.paper_critical_management import PaperCriticalManagementService, _LayerPosition
from app.paper_partial_close_gateway import PaperPartialCloseGateway
from app.paper_pending_reconciler import PaperPendingReconciler
from app.trading_management_canonical import CanonicalTradingManagementService


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


def test_tdc_buy_limits_high_risk_zone_becomes_exact_broker_layer_grid() -> None:
    raw = (
        "BUY LIMITS GOLD @ 4386/4381 AREA\n\n"
        "TP 4389\nTP 4393\nTP 4398\nTP OPEN\nSL 4380\n\nHIGH RISK TRADE"
    )
    entries = parse_critical_entries(
        raw,
        side="BUY",
        entry_low="4381",
        entry_high="4386",
    )
    assert [(item.entry_index, item.order_type, item.price) for item in entries] == [
        (1, "buy_limit", Decimal("4386")),
        (2, "buy_limit", Decimal("4385")),
        (3, "buy_limit", Decimal("4384")),
        (4, "buy_limit", Decimal("4383")),
        (5, "buy_limit", Decimal("4382")),
        (6, "buy_limit", Decimal("4381")),
    ]


def test_tdc_immediate_buy_zone_opens_one_layer_then_uses_pending_retracement_layers() -> None:
    raw = (
        "BUY GOLD @ 4398/4393\n\n"
        "TP 4400\nTP 4403\nTP 4407\nTP OPEN\nSL 4392\n\nHIGH RISK TRADE"
    )
    entries = parse_critical_entries(
        raw,
        side="BUY",
        entry_low="4393",
        entry_high="4398",
    )
    assert [(item.entry_index, item.order_type, item.price) for item in entries] == [
        (1, "market", Decimal("4398")),
        (2, "buy_limit", Decimal("4397")),
        (3, "buy_limit", Decimal("4396")),
        (4, "buy_limit", Decimal("4395")),
        (5, "buy_limit", Decimal("4394")),
        (6, "buy_limit", Decimal("4393")),
    ]


def test_unknown_plural_pending_zone_never_invents_a_grid() -> None:
    with pytest.raises(ValueError, match="pending_layer_grid_unspecified"):
        parse_critical_entries(
            "BUY LIMITS GOLD @ 4332/4326 AREA\nTP 4335\nSL 4325",
            side="BUY",
            entry_low="4326",
            entry_high="4332",
        )


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


def test_tdc_close_table_executes_only_named_layers_and_preserves_best_state_text() -> None:
    raw = (
        "+25\n\nRISK FREEE 4393\n\n"
        "4393 SL TO BE\n"
        "4394 CLOSE +15\n"
        "4395 CLOSE +5\n"
        "4396 CLOSE -0\n"
        "4397 CLOSE +5\n\n"
        "TOTAL CLOSED PROFIT +15 PIPS AND BEST ENTRY STILL RUNNING WITH SL AT BE AT 4393"
    )
    actions = augment_management_actions(
        raw,
        ({"type": "edit_stop_loss", "target": "all", "value": "4393"},),
    )
    assert {"type": "close", "target": "entry_price_4394", "value": None} in actions
    assert {"type": "close", "target": "entry_price_4395", "value": None} in actions
    assert {"type": "close", "target": "entry_price_4396", "value": None} in actions
    assert {"type": "close", "target": "entry_price_4397", "value": None} in actions
    assert {"type": "close", "target": "all_but_best", "value": None} not in actions
    assert {
        "type": "edit_stop_loss",
        "target": "best_entry_risk_free_4393",
        "value": "4393",
    } in actions


def test_tdc_bare_numeric_risk_free_is_not_blanket_stop_on_layered_trade() -> None:
    actions = augment_management_actions(
        "+20\n\nRISK FREEE 4393",
        ({"type": "edit_stop_loss", "target": "all", "value": "4393"},),
    )
    assert actions[0] == {"type": "close", "target": "all_but_best", "value": None}
    assert {
        "type": "edit_stop_loss",
        "target": "best_entry_risk_free_4393",
        "value": "4393",
    } in actions


def test_leave_best_buy_closes_highest_price_layers_only() -> None:
    positions = (
        _position(1, 1, "4350"),
        _position(2, 1, "4346"),
        _position(3, 1, "4342"),
        _position(4, 1, "4338"),
    )
    selected = CanonicalTradingManagementService._select_layer_positions(
        positions,
        "worst_3_layers",
        side="BUY",
    )
    assert {item.entry_index for item in selected} == {1, 2, 3}
    assert {item.entry_index for item in positions if item not in selected} == {4}


def test_best_entry_buy_is_lowest_actual_fill() -> None:
    positions = (
        _position(1, 1, "4397.51"),
        _position(2, 1, "4397"),
        _position(3, 1, "4396"),
        _position(4, 1, "4395"),
        _position(5, 1, "4394"),
        _position(6, 1, "4393"),
    )
    selected = CanonicalTradingManagementService._select_layer_positions(
        positions,
        "best_entry",
        side="BUY",
    )
    assert {item.entry_index for item in selected} == {6}
    worse = CanonicalTradingManagementService._select_layer_positions(
        positions,
        "all_but_best",
        side="BUY",
    )
    assert {item.entry_index for item in worse} == {1, 2, 3, 4, 5}


def test_buy_risk_free_stop_is_allowed_when_best_layer_really_filled_at_that_price() -> None:
    positions = (
        _position(1, 1, "4397.51"),
        _position(2, 1, "4397"),
        _position(3, 1, "4396"),
        _position(4, 1, "4395"),
        _position(5, 1, "4394"),
        _position(6, 1, "4393"),
    )
    selected = CanonicalTradingManagementService._select_layer_positions(
        positions,
        "best_entry_risk_free_4393",
        side="BUY",
    )
    assert {item.entry_index for item in selected} == {6}


def test_buy_risk_free_stop_fails_if_only_worse_layer_has_filled() -> None:
    positions = (_position(1, 1, "4397.51"),)
    with pytest.raises(Day27ManagementError, match="risk_free_stop_not_protective"):
        CanonicalTradingManagementService._select_layer_positions(
            positions,
            "best_entry_risk_free_4393",
            side="BUY",
        )


def test_sell_risk_free_stop_uses_inverse_protection_rule() -> None:
    positions = (
        _position(1, 1, "4393"),
        _position(2, 1, "4394"),
        _position(3, 1, "4395"),
    )
    selected = CanonicalTradingManagementService._select_layer_positions(
        positions,
        "best_entry_risk_free_4395",
        side="SELL",
    )
    assert {item.entry_index for item in selected} == {3}
    with pytest.raises(Day27ManagementError, match="risk_free_stop_not_protective"):
        CanonicalTradingManagementService._select_layer_positions(
            (_position(1, 1, "4393"),),
            "best_entry_risk_free_4395",
            side="SELL",
        )


def test_provider_close_price_maps_to_unique_nearest_actual_layer() -> None:
    positions = (
        _position(1, 1, "4397.51"),
        _position(2, 1, "4397.00"),
        _position(3, 1, "4396.00"),
        _position(4, 1, "4395.00"),
        _position(5, 1, "4394.00"),
        _position(6, 1, "4393.00"),
    )
    selected = CanonicalTradingManagementService._select_layer_positions(
        positions,
        "entry_price_4394",
        side="BUY",
    )
    assert {item.entry_index for item in selected} == {5}


def test_provider_close_price_fails_closed_when_no_layer_is_close_enough() -> None:
    positions = (_position(1, 1, "4397.5"), _position(2, 1, "4396.5"))
    with pytest.raises(Day27ManagementError, match="layer_provider_price_unresolved"):
        CanonicalTradingManagementService._select_layer_positions(
            positions,
            "entry_price_4394",
            side="BUY",
        )


def test_leave_best_sell_closes_lowest_price_layers_only() -> None:
    positions = (
        _position(1, 1, "4350"),
        _position(2, 1, "4354"),
        _position(3, 1, "4358"),
        _position(4, 1, "4362"),
    )
    selected = CanonicalTradingManagementService._select_layer_positions(
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


def test_critical_executor_has_no_aggregate_layer_risk_cap() -> None:
    assert not hasattr(PaperCriticalExecutionService, "_assert_layer_risk_cap")


def test_canonical_pending_gateway_has_no_paper_live_environment_switch() -> None:
    parameters = signature(MetaApiPendingOrderGateway.place_pending_order).parameters
    assert "account_environment" not in parameters


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
