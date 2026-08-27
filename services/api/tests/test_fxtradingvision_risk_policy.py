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
    return BrokerVolumeRules.from_values(
        minimum="0.01", maximum="100", step="0.01"
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


def test_tig_buy_policy_is_exact_4_3_2_1_and_stops_at_tp4() -> None:
    source = "TIG’s Asia Trades"
    assert is_tig_buy(source_name=source, side="BUY")
    assert provider_side_enabled(source_name=source, side="BUY")
    assert provider_tp_limit(source_name=source, side="BUY") == 4
    assert provider_risk_profile(
        source_name=source, side="BUY", position_count=4
    ) == (
        Decimal("4"),
        Decimal("3"),
        Decimal("2"),
        Decimal("1"),
    )


def test_tig_sell_policy_is_exact_5_5_and_stops_at_tp2() -> None:
    source = "TIG’s Asia Trades"
    assert is_tig_sell(source_name=source, side="SELL")
    assert provider_side_enabled(source_name=source, side="SELL")
    assert provider_tp_limit(source_name=source, side="SELL") == 2
    assert provider_risk_profile(
        source_name=source, side="SELL", position_count=2
    ) == (Decimal("5"), Decimal("5"))


@pytest.mark.parametrize(
    "source",
    [
        "FXTradingVision l Forex & Crypto Signals 🚀",
        "GTMO VIP 🤴🏽",
    ],
)
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
            volume_rules=_rules(),
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


def test_tig_buy_never_accepts_a_fifth_leg() -> None:
    with pytest.raises(ValueError, match="tig_buy_position_count_invalid"):
        provider_risk_profile(
            source_name="TIG’s Asia Trades", side="BUY", position_count=5
        )


def test_tig_sell_never_accepts_a_third_leg() -> None:
    with pytest.raises(ValueError, match="tig_sell_position_count_invalid"):
        provider_risk_profile(
            source_name="TIG’s Asia Trades", side="SELL", position_count=3
        )


def test_fx_size_profile_risks_ten_percent_across_three_legs() -> None:
    result = Day24RiskSizer.size_profile(
        balance=Decimal("1500"),
        risk_percents=(Decimal("4"), Decimal("4"), Decimal("2")),
        signal_entry_price=Decimal("100"),
        signal_stop_loss=Decimal("90"),
        tick_size=Decimal("1"),
        tick_value=Decimal("1"),
        volume_rules=_rules(),
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
        volume_rules=_rules(),
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


def test_tig_buy_profile_totals_ten_percent() -> None:
    profile = provider_risk_profile(
        source_name="TIG’s Asia Trades", side="BUY", position_count=4
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
        Decimal("60"),
        Decimal("45"),
        Decimal("30"),
        Decimal("15"),
    ]
    assert result.total_risk_budget == Decimal("150")


def test_tig_sell_profile_accepts_provider_only_five_percent_and_totals_ten() -> None:
    profile = provider_risk_profile(
        source_name="TIG’s Asia Trades", side="SELL", position_count=2
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
        Decimal("75"),
        Decimal("75"),
    ]
    assert result.total_risk_budget == Decimal("150")


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
    gtmo_profile = provider_risk_profile(
        source_name="GTMO VIP 🤴🏽", side="BUY", position_count=3
    )
    tig_profile = provider_risk_profile(
        source_name="TIG’s Asia Trades", side="SELL", position_count=2
    )
    assert gtmo_profile is not None
    assert tig_profile is not None

    for risk in (gtmo_profile[1], tig_profile[0]):
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
