"""GoldThinker launch gate: ONE percent of account value per complete signal.

Includes ALL TP positions and runners. Does not permit broker-minimum rounding
or 'DOUBLE LOT' instructions to bypass the whole-signal cash cap.
"""
from decimal import Decimal

import pytest

from app.provider_risk_policy import provider_risk_profile
from app.risk_sizing_day24 import (
    ApprovedProviderRisk,
    BrokerVolumeRules,
    Day24RiskSizer,
    Day24RiskSizingError,
)


RULES = BrokerVolumeRules.from_values(minimum="0.01", maximum="100", step="0.01")


@pytest.mark.parametrize("account,targets", [
    ("1000", 1), ("1000", 2), ("1000", 3), ("1000", 4),
    ("5000", 1), ("5000", 3),
    ("50000", 1), ("50000", 3), ("50000", 4),
])
def test_one_percent_cash_ceiling_covers_every_target(account: str, targets: int):
    profile = provider_risk_profile(
        source_name="TIG’s Asia Trades", side="BUY", position_count=targets,
    )
    assert profile is not None
    assert all(isinstance(p, ApprovedProviderRisk) for p in profile)
    assert sum(profile, Decimal("0")) <= Decimal("1")

    result = Day24RiskSizer.size_profile(
        balance=account,
        risk_percents=profile,
        signal_entry_price="4000",
        signal_stop_loss="4001",
        tick_size="0.01",
        tick_value="1",
        volume_rules=RULES,
    )
    ceiling = Decimal(account) * Decimal("0.01")
    assert sum((p.risk_budget for p in result.positions), Decimal("0")) <= ceiling
    assert sum((p.actual_risk for p in result.positions), Decimal("0")) <= ceiling
    assert result.total_actual_risk <= ceiling


def test_broker_minimum_cannot_breach_full_trade_one_percent():
    # XAUUSD 0.01 lots and $20 stop: $20 risk per smallest leg.
    # Even on a $1,000 account with a single TP, this exceeds the $10 limit.
    profile = provider_risk_profile(
        source_name="Any Provider", side="SELL", position_count=1,
    )
    assert profile is not None
    with pytest.raises(Day24RiskSizingError, match="broker_minimum_exceeds_risk_budget"):
        Day24RiskSizer.size_profile(
            balance="1000", risk_percents=profile,
            signal_entry_price="4000", signal_stop_loss="4020",
            tick_size="0.01", tick_value="1", volume_rules=RULES,
        )


def test_profile_cannot_inject_more_than_one_percent():
    bad = (ApprovedProviderRisk("0.6"), ApprovedProviderRisk("0.6"))
    with pytest.raises(Day24RiskSizingError, match="signal_risk_budget_exceeded"):
        Day24RiskSizer.size_profile(
            balance="50000", risk_percents=bad,
            signal_entry_price="4000", signal_stop_loss="4001",
            tick_size="0.01", tick_value="1", volume_rules=RULES,
        )


def test_signal_double_lot_instruction_cannot_double_approved_risk():
    budget = provider_risk_profile(
        source_name="TIG’s Asia Trades", side="BUY", position_count=2,
    )
    assert budget is not None
    for leg in budget:
        result = Day24RiskSizer.size(
            balance="1000", risk_percent=leg,
            signal_entry_price="4000", signal_stop_loss="4001",
            tick_size="0.01", tick_value="1", take_profit_count=1,
            volume_rules=RULES, signal_requests_double_lot=True,
            double_lot_approved=True,
        )
        assert result.double_lot_applied is False
        assert result.actual_risk_per_position <= Decimal("5")


def test_invalid_stop_blocks_signal_instead_of_submitting_unprotected_order():
    profile = provider_risk_profile(
        source_name="Any Provider", side="BUY", position_count=2,
    )
    assert profile is not None
    with pytest.raises(Day24RiskSizingError, match="signal_entry_stop_invalid"):
        Day24RiskSizer.size_profile(
            balance="1000", risk_percents=profile,
            signal_entry_price="4000", signal_stop_loss="4000",
            tick_size="0.01", tick_value="1", volume_rules=RULES,
        )
