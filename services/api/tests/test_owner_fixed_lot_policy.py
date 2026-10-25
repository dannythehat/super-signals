"""Regression tests for the owner-approved fixed 0.01-lot execution path."""
from decimal import Decimal

import pytest

from app.provider_risk_policy import (
    FIXED_PROVIDER_LEG_VOLUME, FIXED_PROVIDER_MAX_SIGNAL_RISK_PERCENT,
    FIXED_PROVIDER_MAX_STOP_RISK_PERCENT,
)
from app.risk_sizing_day24 import BrokerVolumeRules, Day24RiskSizer, Day24RiskSizingError


def size(*, balance="1000", entry="4000", stop="4005", count=3,
         minimum="0.01", step="0.01", double=False):
    return Day24RiskSizer.size_fixed_volume(
        balance=balance, signal_entry_price=entry, signal_stop_loss=stop,
        tick_size="0.01", tick_value="1", take_profit_count=count,
        volume_rules=BrokerVolumeRules.from_values(
            minimum=minimum, maximum="100", step=step,
        ),
        fixed_volume=FIXED_PROVIDER_LEG_VOLUME,
        max_stop_risk_percent=FIXED_PROVIDER_MAX_STOP_RISK_PERCENT,
        max_signal_risk_percent=FIXED_PROVIDER_MAX_SIGNAL_RISK_PERCENT,
        signal_requests_double_lot=double,
    )


@pytest.mark.parametrize("balance", ["500", "1000", "5000", "100000"])
@pytest.mark.parametrize("stop", ["4001", "4005", "4010"])
def test_every_leg_exactly_001_independent_of_equity_and_sl(balance, stop):
    result = size(balance=balance, stop=stop)
    assert result.volume == Decimal("0.01")
    assert [p.volume for p in result.positions] == [Decimal("0.01")] * 3
    assert result.total_actual_risk == result.actual_risk_per_position * 3


def test_double_lot_instruction_is_ignored():
    result = size(double=True)
    assert result.signal_requests_double_lot
    assert not result.double_lot_applied
    assert result.volume == Decimal("0.01")


def test_wide_stop_blocks_instead_of_increasing_risk():
    with pytest.raises(Day24RiskSizingError, match="fixed_lot_stop_risk_exceeded"):
        size(balance="100", stop="4010")


def test_total_basket_cap_blocks():
    with pytest.raises(Day24RiskSizingError, match="fixed_lot_signal_risk_exceeded"):
        size(balance="100", stop="4002", count=6)


@pytest.mark.parametrize("minimum,step", [("0.02", "0.01"), ("0.001", "0.003")])
def test_broker_exact_volume_not_supported_blocks(minimum, step):
    with pytest.raises(Day24RiskSizingError, match="fixed_lot_not_supported_by_broker"):
        size(minimum=minimum, step=step)


def test_reports_actual_cash_exposure_not_legacy_one_percent():
    result = size(balance="1000", stop="4005")
    assert result.actual_risk_per_position == Decimal("5")
    assert result.effective_risk_percent == Decimal("0.5")
    assert result.total_actual_risk == Decimal("15")
