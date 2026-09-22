from dataclasses import dataclass
from decimal import Decimal

import pytest

from app.layer_allocation import allocate_entry_targets
from app.provider_risk_policy import (
    POLICY_GENERATION,
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


@dataclass(frozen=True)
class _Entry:
    entry_index: int


def test_provider_policy_generation_is_current_owner_authority() -> None:
    assert POLICY_GENERATION == "owner-authority-2026-09-22-v3-one-percent-total-trade"


@pytest.mark.parametrize(
    ("source", "side"),
    [
        ("FXTradingVision l Forex & Crypto Signals 🚀", "BUY"),
        ("FXTradingVision l Forex & Crypto Signals 🚀", "SELL"),
        ("GTMO VIP 🤴🏽", "BUY"),
        ("TIG’s Asia Trades", "BUY"),
        ("TIG’s Asia Trades", "SELL"),
        ("SureShot GOLD", "SELL"),
        ("United Kings™ Signals! 👑", "SELL"),
        ("Another Provider", "BUY"),
        ("Another Provider", "SELL"),
    ],
)
def test_every_enabled_trade_uses_one_percent_total(source: str, side: str) -> None:
    assert provider_side_enabled(source_name=source, side=side)
    profile = provider_risk_profile(source_name=source, side=side, position_count=4)
    assert profile == (
        Decimal("0.25"),
        Decimal("0.25"),
        Decimal("0.25"),
        Decimal("0.25"),
    )
    assert sum(profile, Decimal("0")) == Decimal("1")


def test_named_provider_identity_helpers_remain_available() -> None:
    assert is_fxtradingvision_buy(
        source_name="FXTradingVision l Forex & Crypto Signals 🚀", side="BUY"
    )
    assert is_gtmo_buy(source_name="GTMO VIP 🤴🏽", side="BUY")
    assert is_tig_buy(source_name="TIG’s Asia Trades", side="BUY")
    assert is_tig_sell(source_name="TIG’s Asia Trades", side="SELL")


def test_existing_direction_eligibility_stays_fail_closed() -> None:
    disabled = [
        ("GTMO VIP 🤴🏽", "SELL"),
        ("SureShot GOLD", "BUY"),
        ("United Kings™ Signals! 👑", "BUY"),
    ]
    for source, side in disabled:
        assert not provider_side_enabled(source_name=source, side=side)
        assert provider_risk_profile(
            source_name=source,
            side=side,
            position_count=3,
        ) == (Decimal("0"), Decimal("0"), Decimal("0"))


def test_fx_keeps_three_target_limit_with_one_percent_total() -> None:
    source = "FXTradingVision l Forex & Crypto Signals 🚀"
    for side in ("BUY", "SELL"):
        assert provider_tp_limit(source_name=source, side=side) == 3
        assert provider_risk_profile(
            source_name=source,
            side=side,
            position_count=3,
        ) == (Decimal("0.3334"), Decimal("0.3333"), Decimal("0.3333"))


def test_tig_has_no_hidden_tp_cap() -> None:
    source = "TIG’s Asia Trades"
    assert provider_tp_limit(source_name=source, side="BUY") is None
    assert provider_tp_limit(source_name=source, side="SELL") is None


def test_four_targets_equal_one_percent_total() -> None:
    profile = provider_risk_profile(
        source_name="TIG’s Asia Trades",
        side="BUY",
        position_count=4,
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
        Decimal("3.7500"),
        Decimal("3.7500"),
        Decimal("3.7500"),
        Decimal("3.7500"),
    ]
    assert result.total_risk_budget == Decimal("15.0000")
    assert result.total_risk_budget / result.balance * Decimal("100") == Decimal("1.00")


def test_two_entries_and_four_targets_still_create_four_target_legs() -> None:
    entries = (_Entry(1), _Entry(2))
    targets = (Decimal("4400"), Decimal("4405"), Decimal("4410"), None)
    plan = allocate_entry_targets(entries, targets)
    assert len(plan) == 4
    assert [(item.entry.entry_index, item.tp_index) for item in plan] == [
        (1, 1),
        (2, 2),
        (1, 3),
        (2, 4),
    ]
    profile = provider_risk_profile(
        source_name="TIG’s Asia Trades",
        side="BUY",
        position_count=len(plan),
    )
    assert profile is not None
    assert sum(profile, Decimal("0")) == Decimal("1")


def test_disabled_profile_still_fails_before_broker_sizing() -> None:
    profile = provider_risk_profile(
        source_name="GTMO VIP 🤴🏽", side="SELL", position_count=1
    )
    assert profile == (Decimal("0"),)
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


def test_locked_one_percent_is_absolute_even_if_signal_says_double_lot() -> None:
    profile = provider_risk_profile(
        source_name="Another Provider", side="BUY", position_count=4
    )
    assert profile is not None
    total = Decimal("0")
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
        assert result.effective_risk_percent == Decimal("0.25")
        assert not result.double_lot_applied
        total += result.effective_risk_percent
    assert total == Decimal("1")
