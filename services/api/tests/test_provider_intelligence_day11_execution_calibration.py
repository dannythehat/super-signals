from decimal import Decimal

import pytest

from app.provider_execution_calibration import (
    CALIBRATION_TOLERANCE_VERSION,
    DEFAULT_RECONCILIATION_TOLERANCE,
    MIN_CALIBRATION_SAMPLES,
    adverse_exit_slippage_points,
    adverse_slippage_points,
    calibration_status,
    execution_adjusted_r,
    intelligence_mode_for_calibration,
    reconciliation_status,
)


def test_entry_slippage_sign_is_adverse_for_both_sides() -> None:
    assert adverse_slippage_points(
        side="BUY", reference=Decimal("4500.00"), actual=Decimal("4500.20")
    ) == Decimal("0.20")
    assert adverse_slippage_points(
        side="SELL", reference=Decimal("4500.00"), actual=Decimal("4499.80")
    ) == Decimal("0.20")


def test_entry_price_improvement_is_negative_cost() -> None:
    assert adverse_slippage_points(
        side="BUY", reference=Decimal("4500.00"), actual=Decimal("4499.90")
    ) == Decimal("-0.10")


def test_exit_slippage_sign_is_adverse_for_both_sides() -> None:
    assert adverse_exit_slippage_points(
        side="BUY", reference=Decimal("4510.00"), actual=Decimal("4509.75")
    ) == Decimal("0.25")
    assert adverse_exit_slippage_points(
        side="SELL", reference=Decimal("4490.00"), actual=Decimal("4490.25")
    ) == Decimal("0.25")


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


def test_reconciliation_tolerance_is_explicit_and_versioned() -> None:
    tolerance = DEFAULT_RECONCILIATION_TOLERANCE
    assert CALIBRATION_TOLERANCE_VERSION == "provider_day11_v1"
    assert tolerance.version == CALIBRATION_TOLERANCE_VERSION
    assert tolerance.min_provider_signals == 5
    assert tolerance.min_total_signals == 30
    assert tolerance.max_median_abs_r_delta == Decimal("0.35")
    assert tolerance.max_p95_abs_r_delta == Decimal("1.00")
    assert tolerance.min_lifecycle_agreement_rate == Decimal("0.80")


def test_reconciliation_fails_closed_for_each_gate() -> None:
    base = {
        "provider_samples": 5,
        "total_samples": 30,
        "median_abs_r_delta": Decimal("0.20"),
        "p95_abs_r_delta": Decimal("0.80"),
        "lifecycle_agreement_rate": Decimal("0.90"),
    }
    assert reconciliation_status(**{**base, "provider_samples": 4}) == (
        "WAITING_INSUFFICIENT_PROVIDER_RECONCILIATION_SAMPLES"
    )
    assert reconciliation_status(**{**base, "total_samples": 29}) == (
        "WAITING_INSUFFICIENT_GLOBAL_RECONCILIATION_SAMPLES"
    )
    assert reconciliation_status(**{**base, "median_abs_r_delta": Decimal("0.36")}) == (
        "WAITING_RECONCILIATION_MEDIAN_R_DIVERGENCE"
    )
    assert reconciliation_status(**{**base, "p95_abs_r_delta": Decimal("1.01")}) == (
        "WAITING_RECONCILIATION_P95_R_DIVERGENCE"
    )
    assert reconciliation_status(
        **{**base, "lifecycle_agreement_rate": Decimal("0.79")}
    ) == "WAITING_RECONCILIATION_LIFECYCLE_DIVERGENCE"
    assert reconciliation_status(**base) == "RECONCILED"


def test_downstream_intelligence_remains_shadow_until_reconciled() -> None:
    waiting_states = (
        "WAITING_INSUFFICIENT_PROVIDER_RECONCILIATION_SAMPLES",
        "WAITING_INSUFFICIENT_GLOBAL_RECONCILIATION_SAMPLES",
        "WAITING_RECONCILIATION_MEDIAN_R_DIVERGENCE",
        "WAITING_RECONCILIATION_P95_R_DIVERGENCE",
        "WAITING_RECONCILIATION_LIFECYCLE_DIVERGENCE",
        "UNKNOWN",
    )
    assert all(intelligence_mode_for_calibration(value) == "SHADOW_WAITING" for value in waiting_states)
    assert intelligence_mode_for_calibration("RECONCILED") == "RESEARCH_READY"


def test_invalid_geometry_side_and_reconciliation_inputs_fail_closed() -> None:
    with pytest.raises(ValueError, match="calibration_side_invalid"):
        adverse_slippage_points(side="FLAT", reference=Decimal("1"), actual=Decimal("1"))
    with pytest.raises(ValueError, match="calibration_risk_distance_invalid"):
        execution_adjusted_r(
            raw_r=Decimal("0"),
            risk_distance=Decimal("0"),
            target_count=1,
            barrier_exit_count=1,
            entry_adverse_points=Decimal("0"),
            exit_adverse_points=Decimal("0"),
        )
    with pytest.raises(ValueError, match="reconciliation_lifecycle_rate_invalid"):
        reconciliation_status(
            provider_samples=5,
            total_samples=30,
            median_abs_r_delta=Decimal("0"),
            p95_abs_r_delta=Decimal("0"),
            lifecycle_agreement_rate=Decimal("1.01"),
        )
