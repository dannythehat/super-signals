"""Safety and target selection checks for read-only PU master management."""
from decimal import Decimal

import pytest
from app.pu_master_management_planner import (
    PuMappedPosition,
    PuManagementBlocked,
    plan_pu_management,
)


def fixture():
    legs = (
        PuMappedPosition("1001", 1, "PU_001_1", "BUY", "XAUUSD.s", Decimal("4500"), Decimal("4490"), Decimal("4510")),
        PuMappedPosition("1002", 2, "PU_001_2", "BUY", "XAUUSD.s", Decimal("4500"), Decimal("4490"), Decimal("4520")),
    )
    live = [{"id": p.ticket, "clientId": p.client_id, "symbol": p.symbol} for p in legs]
    return legs, live


def test_tp1_close_targets_one_atomic_leg():
    legs, live = fixture()
    commands = plan_pu_management(
        event_id="evt1", actions=[{"type": "close", "target": "partial_tp1"}],
        mapped_positions=legs, broker_positions=live,
    )
    assert len(commands) == 1
    assert commands[0].ticket == "1001"
    assert commands[0].payload == {"actionType": "POSITION_CLOSE_ID", "positionId": "1001"}
    assert commands[0].idempotency_key.startswith("pu:management:evt1:")


def test_break_even_preserves_existing_take_profit():
    legs, live = fixture()
    commands = plan_pu_management(
        event_id="evt2", actions=[{"type": "move_to_break_even", "target": "all"}],
        mapped_positions=legs, broker_positions=live,
    )
    assert len(commands) == 2
    assert commands[0].payload["stopLoss"] == 4500
    assert commands[0].payload["takeProfit"] == 4510
    assert commands[1].payload["takeProfit"] == 4520


def test_broker_comment_mismatch_fails_closed():
    legs, live = fixture()
    live[0]["clientId"] = "OTHER_BOT"
    with pytest.raises(PuManagementBlocked, match="ownership_mismatch"):
        plan_pu_management(
            event_id="evt3", actions=[{"type": "close", "target": "TP1"}],
            mapped_positions=legs, broker_positions=live,
        )


def test_edit_sl_cannot_increase_buy_risk():
    legs, live = fixture()
    with pytest.raises(PuManagementBlocked, match="provider_would_widen_stop_loss"):
        plan_pu_management(
            event_id="evt4", actions=[{"type": "edit_stop_loss", "target": "all", "value": 4480}],
            mapped_positions=legs, broker_positions=live,
        )


def test_missing_broker_ticket_requires_reconciliation():
    legs, live = fixture()
    with pytest.raises(PuManagementBlocked, match="reconciliation_required"):
        plan_pu_management(
            event_id="evt5", actions=[{"type": "close", "target": "TP2"}],
            mapped_positions=legs, broker_positions=live[:1],
        )


def test_unmapped_entry_layer_is_not_guessed():
    legs, live = fixture()
    with pytest.raises(PuManagementBlocked, match="unsupported_provider_management_target"):
        plan_pu_management(
            event_id="evt6", actions=[{"type": "close", "target": "entry_2"}],
            mapped_positions=legs, broker_positions=live,
        )

def test_profit_locking_stop_beyond_entry_allowed():
    legs, live = fixture()
    updates = plan_pu_management(
        event_id="evt7",
        actions=[{"type": "edit_stop_loss", "target": "TP2", "value": 4505}],
        mapped_positions=legs,
        broker_positions=live,
    )
    assert len(updates) == 1
    assert updates[0].ticket == "1002"
    assert updates[0].payload["stopLoss"] == 4505
    assert updates[0].payload["takeProfit"] == 4520


def test_sell_stop_tightens_into_profit_without_widening():
    legs, live = fixture()
    short = PuMappedPosition(
        "1001", 1, "PU_001_1", "SELL", "XAUUSD.s",
        Decimal("4500"), Decimal("4510"), Decimal("4490"),
    )
    update = plan_pu_management(
        event_id="evt8",
        actions=[{"type": "edit_stop_loss", "target": "TP1", "value": 4495}],
        mapped_positions=(short,),
        broker_positions=live[:1],
    )
    assert update[0].payload["stopLoss"] == 4495
    assert update[0].payload["takeProfit"] == 4490


def test_sell_widening_stop_fails_closed():
    legs, live = fixture()
    short = PuMappedPosition(
        "1001", 1, "PU_001_1", "SELL", "XAUUSD.s",
        Decimal("4500"), Decimal("4510"), Decimal("4490"),
    )
    with pytest.raises(PuManagementBlocked, match="provider_would_widen_stop_loss"):
        plan_pu_management(
            event_id="evt9",
            actions=[{"type": "edit_stop_loss", "target": "TP1", "value": 4515}],
            mapped_positions=(short,), broker_positions=live[:1],
        )
