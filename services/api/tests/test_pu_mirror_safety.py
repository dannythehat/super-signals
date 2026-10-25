"""PU Prime master mirror fixed-volume risk policy regression tests."""
from decimal import Decimal

import pytest

from app.pu_mirror_safety import MirrorRejected, plan_pu_mirror_leg


def inputs():
    return {
        "source": {
            "symbol": "XAUUSD", "type": "POSITION_TYPE_BUY", "openPrice": 4000,
            "stopLoss": 3998, "takeProfit": 4008,
        },
        "account": {
            "tradeAllowed": True, "currency": "USD", "equity": 112, "freeMargin": 112,
        },
        "quote": {
            "symbol": "XAUUSD.s", "bid": 3999.90, "ask": 4000.10,
            "lossTickValue": 1,
        },
        "specification": {
            "symbol": "XAUUSD.s", "minVolume": 0.01, "maxVolume": 100,
            "volumeStep": 0.01, "tickSize": 0.01,
        },
        "calculated_margin": 9,
        "source_signal_legs": 3,
    }


def test_exact_001_per_tp_small_pu_account_only_when_risk_fits():
    plan = plan_pu_mirror_leg(**inputs())
    assert plan.volume == Decimal("0.01")
    assert plan.stop_risk_usd == Decimal("2.10")
    assert plan.symbol == "XAUUSD.s"


def test_pu_broker_equity_prohibits_typical_wide_stops_with_112():
    v = inputs()
    v["source"]["stopLoss"] = 3988
    with pytest.raises(MirrorRejected, match="fixed_lot_stop_risk_exceeded"):
        plan_pu_mirror_leg(**v)


def test_small_balance_never_resizes_to_smaller_lots():
    v = inputs()
    v["account"]["equity"] = 40
    with pytest.raises(MirrorRejected, match="fixed_lot_stop_risk_exceeded"):
        plan_pu_mirror_leg(**v)


def test_different_gold_symbol_is_rejected():
    v = inputs()
    v["quote"]["symbol"] = "XAUUSD"
    with pytest.raises(MirrorRejected, match="pu_gold_symbol_mismatch"):
        plan_pu_mirror_leg(**v)


def test_risk_sum_counts_other_open_pu_positions():
    v = inputs()
    v["existing_risk_usd"] = 10
    with pytest.raises(MirrorRejected, match="pu_total_open_stop_risk_exceeded"):
        plan_pu_mirror_leg(**v)


def test_source_broker_quote_is_not_chased():
    v = inputs()
    v["quote"]["ask"] = 4004
    with pytest.raises(MirrorRejected, match="copy_slippage_above_limit"):
        plan_pu_mirror_leg(**v)


def test_never_remove_stop_or_follow_expired_tp():
    v = inputs()
    v["source"]["stopLoss"] = None
    with pytest.raises(MirrorRejected):
        plan_pu_mirror_leg(**v)
    v = inputs()
    v["source"]["takeProfit"] = 3999
    with pytest.raises(MirrorRejected, match="source_target_already_passed"):
        plan_pu_mirror_leg(**v)


def test_margin_buffer_and_disabled_account_fail_closed():
    v = inputs()
    v["calculated_margin"] = 100
    with pytest.raises(MirrorRejected, match="pu_margin_insufficient_with_buffer"):
        plan_pu_mirror_leg(**v)
    v = inputs()
    v["account"]["tradeAllowed"] = False
    with pytest.raises(MirrorRejected, match="pu_account_not_tradable_usd"):
        plan_pu_mirror_leg(**v)
