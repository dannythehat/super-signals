from decimal import Decimal

import pytest

from app.provider_risk_policy import (
    is_fxtradingvision_buy,
    is_gtmo_buy,
    is_tig_buy,
    is_tig_sell,
    provider_risk_profile,
    provider_side_enabled,
    provider_tp_limit,
)
from app.risk_sizing_day24 import (
    BrokerVolumeRules,
    Day24RiskSizer,
    Day24RiskSizingError,
)


def _rules() -> BrokerVolumeRules:
    return BrokerVolumeRules.from_values(minimum="0.01", maximum="100", step="0.01")


def test_fxtradingvision_buy_is_best_tier_and_sell_is_medium_tier() -> None:
    source = "FXTradingVision l Forex & Crypto Signals 🚀"
    assert is_fxtradingvision_buy(source_name=source, side="BUY")
    assert provider_side_enabled(source_name=source, side="BUY")
    assert provider_side_enabled(source_name=source, side="SELL")
    assert provider_tp_limit(source_name=source, side="BUY") == 3
    assert provider_tp_limit(source_name=source, side="SELL") == 3
    assert provider_risk_profile(
        source_name=source, side="BUY", position_count=3
    ) == (Decimal("2"), Decimal("2"), Decimal("0.5"))
    assert provider_risk_profile(
        source_name=source, side="SELL", position_count=3
    ) == (Decimal("1"), Decimal("1"), Decimal("0.5"))


def test_gtmo_buy_is_medium_tier_with_half_percent_tail() -> None:
    source = "GTMO VIP 🤴🏽"
    assert is_gtmo_buy(source_name=source, side="BUY")
    assert provider_side_enabled(source_name=source, side="BUY")
    assert provider_tp_limit(source_name=source, side="BUY") is None
    assert provider_risk_profile(
        source_name=source, side="BUY", position_count=5
    ) == (
        Decimal("1"),
        Decimal("1"),
        Decimal("0.5"),
        Decimal("0.5"),
        Decimal("0.5"),
    )


def test_tig_buy_uses_one_percent_for_all_positions() -> None:
    source = "TIG’s Asia Trades"
    assert is_tig_buy(source_name=source, side="BUY")
    assert provider_side_enabled(source_name=source, side="BUY")
    assert provider_tp_limit(source_name=source, side="BUY") is None
    assert provider_risk_profile(
        source_name=source, side="BUY", position_count=4
    ) == (Decimal("1"), Decimal("1"), Decimal("1"), Decimal("1"))


def test_tig_sell_uses_one_percent_for_all_positions() -> None:
    source = "TIG’s Asia Trades"
    assert is_tig_sell(source_name=source, side="SELL")
    assert provider_side_enabled(source_name=source, side="SELL")
    assert provider_tp_limit(source_name=source, side="SELL") is None
    assert provider_risk_profile(
        source_name=source, side="SELL", position_count=4
    ) == (Decimal("1"), Decimal("1"), Decimal("1"), Decimal("1"))


def test_gtmo_sell_remains_disabled_and_fails_closed() -> None:
    source = "GTMO VIP 🤴🏽"
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
            volume_rules=_rules(),
        )


def test_sureshot_sell_is_medium_tier_and_buy_is_disabled() -> None:
    source = "SureShot GOLD"
    assert provider_side_enabled(source_name=source, side="SELL")
    assert not provider_side_enabled(source_name=source, side="BUY")
    assert provider_risk_profile(
        source_name=source, side="SELL", position_count=4
    ) == (Decimal("1"), Decimal("1"), Decimal("0.5"), Decimal("0.5"))
    assert provider_risk_profile(
        source_name=source, side="BUY", position_count=2
    ) == (Decimal("0"), Decimal("0"))


def test_united_kings_sell_is_developing_half_percent_tier_and_buy_is_disabled() -> None:
    source = "United Kings™ Signals! 👑"
    assert provider_side_enabled(source_name=source, side="SELL")
    assert not provider_side_enabled(source_name=source, side="BUY")
    assert provider_risk_profile(
        source_name=source, side="SELL", position_count=3
    ) == (Decimal("0.5"), Decimal("0.5"), Decimal("0.5"))
    assert provider_risk_profile(
        source_name=source, side="BUY", position_count=3
    ) == (Decimal("0"), Decimal("0"), Decimal("0"))


def test_unlisted_provider_keeps_standard_policy() -> None:
    assert provider_side_enabled(source_name="Another Provider", side="SELL")
    assert provider_risk_profile(
        source_name="Another Provider", side="SELL", position_count=3
    ) is None


def test_fx_never_creates_a_fourth_tp_leg_for_buy_or_sell() -> None:
    for side in ("BUY", "SELL"):
        with pytest.raises(ValueError, match="fxtradingvision_position_count_invalid"):
            provider_risk_profile(source_name="FXTradingVision", side=side, position_count=4)


def test_fx_buy_profile_totals_four_and_a_half_percent() -> None:
    profile = provider_risk_profile(
        source_name="FXTradingVision", side="BUY", position_count=3
    )
    assert profile is not None
    result = Day24RiskSizer.size_profile(
        balance=Decimal("1500"),
        risk_percents=profile,
        signal_entry_price=Decimal("100"),
        signal_stop_loss=Decimal("90"),
        tick_size=Decimal("1"),
        tick_value=Decimal("1"),
        volume_rules=_rules(),
    )
    assert [item.risk_budget for item in result.positions] == [
        Decimal("30"), Decimal("30"), Decimal("7.5")
    ]
    assert result.total_risk_budget == Decimal("67.5")


def test_fx_sell_profile_totals_two_and_a_half_percent() -> None:
    profile = provider_risk_profile(
        source_name="FXTradingVision", side="SELL", position_count=3
    )
    assert profile is not None
    result = Day24RiskSizer.size_profile(
        balance=Decimal("1500"),
        risk_percents=profile,
        signal_entry_price=Decimal("100"),
        signal_stop_loss=Decimal("90"),
        tick_size=Decimal("1"),
        tick_value=Decimal("1"),
        volume_rules=_rules(),
    )
    assert [item.risk_budget for item in result.positions] == [
        Decimal("15"), Decimal("15"), Decimal("7.5")
    ]
    assert result.total_risk_budget == Decimal("37.5")


def test_gtmo_five_leg_profile_totals_three_and_a_half_percent() -> None:
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
        volume_rules=_rules(),
    )
    assert [item.risk_budget for item in result.positions] == [
        Decimal("15"), Decimal("15"), Decimal("7.5"), Decimal("7.5"), Decimal("7.5")
    ]
    assert result.total_risk_budget == Decimal("52.5")


def test_tig_buy_profile_is_one_percent_per_leg() -> None:
    profile = provider_risk_profile(
        source_name="TIG’s Asia Trades", side="BUY", position_count=3
    )
    assert profile is not None
    result = Day24RiskSizer.size_profile(
        balance=Decimal("1500"),
        risk_percents=profile,
        signal_entry_price=Decimal("100"),
        signal_stop_loss=Decimal("90"),
        tick_size=Decimal("1"),
        tick_value=Decimal("1"),
        volume_rules=_rules(),
    )
    assert [item.risk_budget for item in result.positions] == [
        Decimal("15"), Decimal("15"), Decimal("15")
    ]
    assert result.total_risk_budget == Decimal("45")


def test_tig_sell_profile_is_one_percent_per_leg() -> None:
    profile = provider_risk_profile(
        source_name="TIG’s Asia Trades", side="SELL", position_count=3
    )
    assert profile is not None
    result = Day24RiskSizer.size_profile(
        balance=Decimal("1500"),
        risk_percents=profile,
        signal_entry_price=Decimal("100"),
        signal_stop_loss=Decimal("90"),
        tick_size=Decimal("1"),
        tick_value=Decimal("1"),
        volume_rules=_rules(),
    )
    assert [item.risk_budget for item in result.positions] == [
        Decimal("15"), Decimal("15"), Decimal("15")
    ]
    assert result.total_risk_budget == Decimal("45")


@pytest.mark.parametrize("risk_percent", [Decimal("3"), Decimal("5")])
def test_provider_only_risks_remain_invalid_as_general_user_risk(
    risk_percent: Decimal,
) -> None:
    with pytest.raises(Day24RiskSizingError, match="risk_percent_invalid"):
        Day24RiskSizer.size(
            balance=Decimal("1500"),
            risk_percent=risk_percent,
            signal_entry_price=Decimal("100"),
            signal_stop_loss=Decimal("90"),
            tick_size=Decimal("1"),
            tick_value=Decimal("1"),
            take_profit_count=1,
            volume_rules=_rules(),
        )


def test_locked_provider_risk_is_absolute_even_if_signal_says_double_lot() -> None:
    profile = provider_risk_profile(
        source_name="FXTradingVision", side="BUY", position_count=3
    )
    assert profile is not None
    for risk in profile:
        result = Day24RiskSizer.size(
            balance=Decimal("1500"),
            risk_percent=risk,
            signal_entry_price=Decimal("100"),
            signal_stop_loss=Decimal("90"),
            tick_size=Decimal("1"),
            tick_value=Decimal("1"),
            take_profit_count=1,
            volume_rules=_rules(),
            signal_requests_double_lot=True,
            double_lot_approved=True,
        )
        assert result.effective_risk_percent == Decimal(str(risk))
        assert not result.double_lot_applied
