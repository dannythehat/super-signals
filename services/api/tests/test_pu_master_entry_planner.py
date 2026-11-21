"""Read-only PU master planning with fixed 0.01 lots per target."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from dataclasses import replace

import pytest

from app.pu_master_entry_planner import PuPlanBlocked, plan_pu_entry


def sample():
    now = datetime.now(timezone.utc)
    return {
        "signal_id": "test-signal",
        "symbol": "XAUUSD",
        "side": "BUY",
        "order_type": "market",
        "parser_status": "accepted",
        "source_status": "testing",
        "source_name": "Asia Trader",
        "source_posted_at": now,
        "entry_low": 4500,
        "entry_high": 4500,
        "stop_loss": 4490,
        "take_profits": (4510, 4520, 4530),
        "has_open_runner": False,
        "account": {"equity": 10000, "currency": "USD", "tradeAllowed": True},
        "price": {"bid": 4500, "ask": 4500.5, "lossTickValue": 1.0},
        "specification": {
            "tickSize": 0.01, "minVolume": 0.01,
            "maxVolume": 100.0, "volumeStep": 0.01,
        },
        "now": now,
    }


def test_exact_001_lots_per_tp_and_no_mutations():
    plan = plan_pu_entry(**sample())
    assert len(plan.legs) == 3
    assert {p.tp_index for p in plan.legs} == {1, 2, 3}
    assert all(p.volume == Decimal("0.01") for p in plan.legs)
    assert all(p.risk_usd <= Decimal("100") for p in plan.legs)
    assert plan.total_risk_usd <= Decimal("300")
    assert plan.symbol == "XAUUSD.s"
    assert plan.automatic_trading_enabled is False


def test_crossed_tp_skipped_without_reindexing():
    args = sample()
    args["price"]["ask"] = 4515
    plan = plan_pu_entry(**args)
    assert tuple(p.tp_index for p in plan.legs) == (2, 3)


@pytest.mark.parametrize("status", ["shadow", "paused", "revoked"])
def test_shadow_and_paused_cannot_execute(status):
    args = sample()
    args["source_status"] = status
    with pytest.raises(PuPlanBlocked, match="signal_not_approved_for_live"):
        plan_pu_entry(**args)


def test_broker_minimum_risk_violation_fails_closed():
    args = sample()
    args["account"]["equity"] = 10
    with pytest.raises(PuPlanBlocked, match="fixed_lot_stop_risk_exceeded"):
        plan_pu_entry(**args)


def test_wrong_direction_stops_are_blocked():
    args = sample()
    args["stop_loss"] = 4520
    with pytest.raises(PuPlanBlocked, match="stop_loss_already_crossed"):
        plan_pu_entry(**args)


def test_disabled_provider_direction_is_not_tradable():
    args = sample()
    args["source_name"] = "Sureshot"
    with pytest.raises(PuPlanBlocked, match="provider_direction_disabled"):
        plan_pu_entry(**args)


def test_old_signal_does_not_backfill_live_order():
    args = sample()
    args["source_posted_at"] -= timedelta(minutes=3)
    with pytest.raises(PuPlanBlocked, match="signal_no_longer_fresh"):
        plan_pu_entry(**args)


def test_selling_tps_and_stop_direction():
    args = sample()
    args.update({"side": "SELL", "stop_loss": 4510, "take_profits": (4490, 4480)})
    plan = plan_pu_entry(**args)
    assert len(plan.legs) == 2
    assert plan.side == "SELL"


def test_trading_disabled_never_plans():
    args = sample()
    args["account"]["tradeAllowed"] = False
    with pytest.raises(PuPlanBlocked, match="broker_trading_disabled"):
        plan_pu_entry(**args)


@pytest.mark.parametrize("equity", [500, 1000, 5000, 10000])
def test_pu_lot_size_does_not_grow_with_master_equity(equity):
    args = sample()
    args["account"]["equity"] = equity
    plan = plan_pu_entry(**args)
    assert [p.volume for p in plan.legs] == [Decimal("0.01")] * 3


def test_pu_broker_minimum_above_001_fails_closed():
    args = sample()
    args["specification"]["minVolume"] = 0.02
    with pytest.raises(PuPlanBlocked, match="fixed_lot_not_supported_by_broker"):
        plan_pu_entry(**args)


def test_pu_multiple_leg_risk_guard_preserved():
    args = sample()
    args["account"]["equity"] = 100
    with pytest.raises(PuPlanBlocked, match="fixed_lot_stop_risk_exceeded"):
        plan_pu_entry(**args)
