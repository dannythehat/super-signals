from decimal import Decimal

import pytest

from app.provider_execution_calibration import (
    MIN_CALIBRATION_SAMPLES,
    adverse_exit_slippage_points,
    adverse_slippage_points,
    calibration_status,
    execution_adjusted_r,
)


def test_entry_slippage_sign_is_adverse_for_both_sides() -> None:
    assert adverse_slippage_points(
        side="BUY", reference=Decimal("4400"), actual=Decimal("4400.25")
    ) == Decimal("0.25")
    assert adverse_slippage_points(
        side="SELL", reference=Decimal("4400"), actual=Decimal("4399.75")
    ) == Decimal("0.25")


def test_entry_price_improvement_is_negative_cost() -> None:
    assert adverse_slippage_points(
        side="BUY", reference=Decimal("4400"), actual=Decimal("4399.80")
    ) == Decimal("-0.20")
    assert adverse_slippage_points(
        side="SELL", reference=Decimal("4400"), actual=Decimal("4400.20")
    ) == Decimal("-0.20")


def test_exit_slippage_sign_is_adverse_for_both_sides() -> None:
    assert adverse_exit_slippage_points(
        side="BUY", reference=Decimal("4410"), actual=Decimal("4409.70")
    ) == Decimal("0.30")
    assert adverse_exit_slippage_points(
        side="SELL", reference=Decimal("4390"), actual=Decimal("4390.30")
    ) == Decimal("0.30")


def test_spread_aware_r_only_pays_additional_broker_layer() -> None:
    adjusted = execution_adjusted_r(
        raw_r=Decimal("3.0"),
        risk_distance=Decimal("10"),
        target_count=3,
        barrier_exit_count=2,
        entry_adverse_points=Decimal("0.20"),
        exit_adverse_points=Decimal("0.10"),
        cash_charge_r=Decimal("0.05"),
    )
    # (3*0.20 + 2*0.10)/10 = 0.08R additional slippage; spread is not charged again.
    assert adjusted == Decimal("2.87")


def test_calibration_fails_closed_below_sample_floor() -> None:
    assert MIN_CALIBRATION_SAMPLES == 30
    assert calibration_status(entry_samples=29, exit_samples=100, needs_exit=True) == (
        "WAITING_INSUFFICIENT_ENTRY_SAMPLES"
    )
    assert calibration_status(entry_samples=30, exit_samples=29, needs_exit=True) == (
        "WAITING_INSUFFICIENT_EXIT_SAMPLES"
    )
    assert calibration_status(entry_samples=30, exit_samples=0, needs_exit=False) == (
        "ENGINEERING_CALIBRATED"
    )


def test_invalid_geometry_and_side_fail_closed() -> None:
    with pytest.raises(ValueError, match="calibration_side_invalid"):
        adverse_slippage_points(
            side="FLAT", reference=Decimal("1"), actual=Decimal("1")
        )
    with pytest.raises(ValueError, match="calibration_risk_distance_invalid"):
        execution_adjusted_r(
            raw_r=Decimal("1"),
            risk_distance=Decimal("0"),
            target_count=1,
            barrier_exit_count=1,
            entry_adverse_points=Decimal("0"),
            exit_adverse_points=Decimal("0"),
        )
    with pytest.raises(ValueError, match="calibration_leg_count_invalid"):
        execution_adjusted_r(
            raw_r=Decimal("1"),
            risk_distance=Decimal("10"),
            target_count=1,
            barrier_exit_count=2,
            entry_adverse_points=Decimal("0"),
            exit_adverse_points=Decimal("0"),
        )
