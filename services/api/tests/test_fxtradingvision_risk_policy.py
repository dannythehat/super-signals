from decimal import Decimal

import pytest

from app.provider_risk_policy import (
    is_fxtradingvision_buy,
    is_gtmo_buy,
    provider_risk_profile,
    provider_side_enabled,
    provider_tp_limit,
)
from app.risk_sizing_day24 import (
    BrokerVolumeRules,
    Day24RiskSizer,
    Day24RiskSizingError,
)


def test_fxtradingvision_buy_policy_is_exact_4_4_2() -> None:
    source = "FXTradingVision l Forex & Crypto Signals 🚀"
    assert is_fxtradingvision_buy(source_name=source, side="BUY")
    assert provider_side_enabled(source_name=source, side="BUY")
    assert provider_tp_limit(source_name=source, side="BUY") == 3
    assert provider_risk_profile(
        source_name=source, side="BUY", position_count=3
    ) == (Decimal("4"), Decimal("4"), Decimal("2"))


def test_gtmo_buy_policy_is_4_3_2_then_half_percent_per_extra_leg() -> None:
    source = "GTMO VIP 🤴🏽"
    assert is_gtmo_buy(source_name=source, side="BUY")
    assert provider_side_enabled(source_name=source, side="BUY")
    assert provider_tp_limit(source_name=source, side="BUY") is None
    assert provider_risk_profile(
        source_name=source, side="BUY", position_count=5
    ) == (
        Decimal("4"),
        Decimal("3"),
        Decimal("2"),
        Decimal("0.5"),
        Decimal("0.5"),
    )


@pytest.mark.parametrize("source", [
    "FXTradingVision l Forex & Crypto Signals 🚀",
    "GTMO VIP 🤴🏽",
])
def test_owner_disabled_provider_sells_fail_closed(source: str) -> None:
    assert not provider_side_enabled(source_name=source, side="SELL")
    profile = provider_risk_profile(source_name=source, side="SELL", position_count=3)
    assert profile == (Decimal("0"), Decimal("0"), Decimal("0"))

    with pytest.raises(Day24RiskSizingError, match="risk_percent_invalid"):
        Day24RiskSizer.size(
            balance=Decimal("1500"),
            risk_percent=profile[0],
            signal_entry_price=Decimal("100"),
            signal_stop_loss=Decimal("90"),
            tick_size=Decimal("1"),
            tick_value=Decimal("1"),
            take_profit_count=1,
            volume_rules=BrokerVolumeRules.from_values(
                minimum="0.01", maximum="100", step="0.01"
            ),
        )


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


def test_fx_size_profile_risks_ten_percent_across_three_legs() -> None:
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
    assert result.total_risk_budget == Decimal("150")
    assert result.position_count == 3
    assert not result.double_lot_applied


def test_gtmo_five_leg_profile_accepts_three_percent_and_totals_ten_percent() -> None:
    profile = provider_risk_profile(
        source_name="GTMO VIP 🤴🏽", side="BUY", position_count=5
    )
    assert profile is not None
    result = Day24RiskSizer.size_profile(
        balance=Decimal("1500"),
        risk_percents=profile,
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
        Decimal("45"),
        Decimal("30"),
        Decimal("7.5"),
        Decimal("7.5"),
    ]
    assert result.total_risk_budget == Decimal("150.0")
    assert result.position_count == 5
    assert not result.double_lot_applied
