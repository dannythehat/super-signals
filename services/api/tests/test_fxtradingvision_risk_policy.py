from decimal import Decimal

import pytest

from app.provider_risk_policy import (
    is_fxtradingvision,
    is_fxtradingvision_buy,
    provider_risk_profile,
    provider_tp_limit,
)
from app.risk_sizing_day24 import BrokerVolumeRules, Day24RiskSizer


def test_fxtradingvision_buy_and_sell_policy_is_exact_5_5_1() -> None:
    source = "FXTradingVision l Forex & Crypto Signals 🚀"
    assert is_fxtradingvision(source_name=source, side="BUY")
    assert is_fxtradingvision(source_name=source, side="SELL")
    assert is_fxtradingvision_buy(source_name=source, side="BUY")
    assert not is_fxtradingvision_buy(source_name=source, side="SELL")

    for side in ("BUY", "SELL"):
        assert provider_tp_limit(source_name=source, side=side) == 3
        assert provider_risk_profile(
            source_name=source, side=side, position_count=3
        ) == (Decimal("5"), Decimal("5"), Decimal("1"))


def test_fx_policy_does_not_touch_other_providers() -> None:
    assert provider_risk_profile(
        source_name="GTMO VIP", side="BUY", position_count=3
    ) is None
    assert provider_risk_profile(
        source_name="GTMO VIP", side="SELL", position_count=3
    ) is None


def test_fx_never_creates_a_fourth_tp_leg_for_buy_or_sell() -> None:
    for side in ("BUY", "SELL"):
        with pytest.raises(ValueError, match="fxtradingvision_position_count_invalid"):
            provider_risk_profile(
                source_name="FXTradingVision", side=side, position_count=4
            )


def test_size_profile_risks_eleven_percent_across_three_legs() -> None:
    result = Day24RiskSizer.size_profile(
        balance=Decimal("1500"),
        risk_percents=(Decimal("5"), Decimal("5"), Decimal("1")),
        signal_entry_price=Decimal("100"),
        signal_stop_loss=Decimal("90"),
        tick_size=Decimal("1"),
        tick_value=Decimal("1"),
        volume_rules=BrokerVolumeRules.from_values(
            minimum="0.01", maximum="100", step="0.01"
        ),
    )

    assert [item.risk_budget for item in result.positions] == [
        Decimal("75"),
        Decimal("75"),
        Decimal("15"),
    ]
    assert [item.volume for item in result.positions] == [
        Decimal("7.50"),
        Decimal("7.50"),
        Decimal("1.50"),
    ]
    assert result.total_risk_budget == Decimal("165")
    assert result.total_actual_risk == Decimal("165.00")
    assert result.position_count == 3
    assert not result.double_lot_applied
