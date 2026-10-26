"""PU Prime mirror owner-authorised uncapped risk and fixed-volume regression tests."""
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


def test_exact_001_per_tp_small_pu_account():
    plan = plan_pu_mirror_leg(**inputs())
    assert plan.volume == Decimal("0.01")
    assert plan.stop_risk_usd == Decimal("2.10")
    assert plan.symbol == "XAUUSD.s"


def test_wide_stop_is_not_rejected_by_percentage_risk_cap():
    v = inputs()
    v["source"]["stopLoss"] = 3988
    plan = plan_pu_mirror_leg(**v)
    assert plan.volume == Decimal("0.01")
    assert plan.stop_risk_usd == Decimal("12.10")
    assert plan.stop_loss == Decimal("3988")


def test_small_balance_preserves_exact_lot_even_if_percentage_high():
    v = inputs()
    v["account"]["equity"] = 40
    v["source"]["stopLoss"] = 3988
    plan = plan_pu_mirror_leg(**v)
    assert plan.volume == Decimal("0.01")
    assert plan.stop_risk_usd == Decimal("12.10")


def test_different_gold_symbol_is_rejected():
    v = inputs()
    v["quote"]["symbol"] = "XAUUSD"
    with pytest.raises(MirrorRejected, match="pu_gold_symbol_mismatch"):
        plan_pu_mirror_leg(**v)


def test_existing_risk_is_not_a_percentage_veto():
    v = inputs()
    v["existing_risk_usd"] = 100
    plan = plan_pu_mirror_leg(**v)
    assert plan.volume == Decimal("0.01")


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


def test_actual_broker_margin_and_disabled_account_fail_closed():
    v = inputs()
    v["calculated_margin"] = 100
    assert plan_pu_mirror_leg(**v).volume == Decimal("0.01")
    v["calculated_margin"] = 113
    with pytest.raises(MirrorRejected, match="pu_margin_insufficient"):
        plan_pu_mirror_leg(**v)
    v = inputs()
    v["account"]["tradeAllowed"] = False
    with pytest.raises(MirrorRejected, match="pu_account_not_tradable_usd"):
        plan_pu_mirror_leg(**v)
