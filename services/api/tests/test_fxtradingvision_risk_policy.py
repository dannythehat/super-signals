from decimal import Decimal

import pytest

from app.provider_risk_policy import (
    is_fxtradingvision_buy,
    provider_risk_profile,
    provider_side_enabled,
    provider_tp_limit,
)
from app.risk_sizing_day24 import BrokerVolumeRules, Day24RiskSizer


def test_fxtradingvision_buy_policy_is_exact_4_4_2() -> None:
    source = "FXTradingVision l Forex & Crypto Signals 🚀"
    assert is_fxtradingvision_buy(source_name=source, side="BUY")
    assert provider_side_enabled(source_name=source, side="BUY")
    assert provider_tp_limit(source_name=source, side="BUY") == 3
    assert provider_risk_profile(
        source_name=source, side="BUY", position_count=3
    ) == (Decimal("4"), Decimal("4"), Decimal("2"))


@pytest.mark.parametrize("source", [
    "FXTradingVision l Forex & Crypto Signals 🚀",
    "GTMO VIP",
])
def test_owner_disabled_provider_sells_fail_closed(source: str) -> None:
    assert not provider_side_enabled(source_name=source, side="SELL")
    with pytest.raises(ValueError, match="provider_sell_disabled"):
        provider_risk_profile(source_name=source, side="SELL", position_count=3)


def test_other_provider_sell_keeps_standard_policy() -> None:
    assert provider_side_enabled(source_name="Another Provider", side="SELL")
    assert provider_risk_profile(
        source_name="Another Provider", side="SELL", position_count=3
    ) is None


def test_fx_buy_never_creates_a_fourth_tp_leg() -> None:
    with pytest.raises(ValueError, match="fxtradingvision_buy_position_count_invalid"):
        provider_risk_profile(
            source_name="FXTradingVision", side="BUY", position_count=4
        )


def test_size_profile_risks_ten_percent_across_three_legs() -> None:
    result = Day24RiskSizer.size_profile(
        balance=Decimal("1500"),
        risk_percents=(Decimal("4"), Decimal("4"), Decimal("2")),
        signal_entry_price=Decimal("100"),
        signal_stop_loss=Decimal("90"),
        tick_size=Decimal("1"),
        tick_value=Decimal("1"),
        volume_rules=BrokerVolumeRules.from_values(
            minimum="0.01", maximum="100", step="0.01"
        ),
    )

    assert [item.risk_budget for item in result.positions] == [
        Decimal("60"),
        Decimal("60"),
        Decimal("30"),
    ]
    assert [item.volume for item in result.positions] == [
        Decimal("6.00"),
        Decimal("6.00"),
        Decimal("3.00"),
    ]
    assert result.total_risk_budget == Decimal("150")
    assert result.total_actual_risk == Decimal("150.00")
    assert result.position_count == 3
    assert not result.double_lot_applied
