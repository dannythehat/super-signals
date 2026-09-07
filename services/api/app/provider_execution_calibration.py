"""Provider Intelligence Day 11 execution-cost and reconciliation helpers.

These functions are deliberately pure research math. They do not call MetaAPI, mutate
positions, change sizing, or grant broker authority. Positive slippage means worse
execution for the provider side. Reconciliation is an engineering gate only: anything
outside the explicit versioned tolerance remains shadow/WAITING.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

MIN_CALIBRATION_SAMPLES = 30
CALIBRATION_TOLERANCE_VERSION = "provider_day11_v1"


@dataclass(frozen=True, slots=True)
class ReconciliationTolerance:
    """Versioned paper-to-broker engineering tolerance.

    This is not a trading-edge or promotion threshold. It only answers whether the
    paper instrument is close enough to broker truth to be used by later research.
    The per-provider floor is intentionally smaller than the global floor because all
    five established providers must be represented, while the combined corpus must
    still clear the existing 30-sample engineering minimum.
    """

    version: str = CALIBRATION_TOLERANCE_VERSION
    min_provider_signals: int = 5
    min_total_signals: int = MIN_CALIBRATION_SAMPLES
    max_median_abs_r_delta: Decimal = Decimal("0.35")
    max_p95_abs_r_delta: Decimal = Decimal("1.00")
    min_lifecycle_agreement_rate: Decimal = Decimal("0.80")


DEFAULT_RECONCILIATION_TOLERANCE = ReconciliationTolerance()


def adverse_slippage_points(*, side: str, reference: Decimal, actual: Decimal) -> Decimal:
    """Return signed adverse slippage in XAUUSD points.

    Positive means the actual execution was worse than the reference. Negative means
    price improvement. BUY entries/exits and SELL entries/exits use opposite arithmetic,
    so callers should pass the correct economic reference for the action being measured.
    """
    normalized = side.strip().upper()
    if normalized == "BUY":
        return actual - reference
    if normalized == "SELL":
        return reference - actual
    raise ValueError("calibration_side_invalid")


def adverse_exit_slippage_points(
    *, side: str, reference: Decimal, actual: Decimal
) -> Decimal:
    """Return signed adverse slippage for an exit."""
    normalized = side.strip().upper()
    if normalized == "BUY":
        return reference - actual
    if normalized == "SELL":
        return actual - reference
    raise ValueError("calibration_side_invalid")


def execution_adjusted_r(
    *,
    raw_r: Decimal,
    risk_distance: Decimal,
    target_count: int,
    barrier_exit_count: int,
    entry_adverse_points: Decimal,
    exit_adverse_points: Decimal,
    cash_charge_r: Decimal = Decimal("0"),
) -> Decimal:
    """Apply modeled broker-layer costs to a spread-aware shadow R result."""
    if risk_distance <= 0:
        raise ValueError("calibration_risk_distance_invalid")
    if target_count < 0 or barrier_exit_count < 0 or barrier_exit_count > target_count:
        raise ValueError("calibration_leg_count_invalid")
    slippage_r = (
        Decimal(target_count) * entry_adverse_points
        + Decimal(barrier_exit_count) * exit_adverse_points
    ) / risk_distance
    return raw_r - slippage_r - cash_charge_r


def calibration_status(*, entry_samples: int, exit_samples: int, needs_exit: bool) -> str:
    """Fail closed until the empirical broker corpus clears the minimum sample floor."""
    if entry_samples < MIN_CALIBRATION_SAMPLES:
        return "WAITING_INSUFFICIENT_ENTRY_SAMPLES"
    if needs_exit and exit_samples < MIN_CALIBRATION_SAMPLES:
        return "WAITING_INSUFFICIENT_EXIT_SAMPLES"
    return "ENGINEERING_CALIBRATED"


def reconciliation_status(
    *,
    provider_samples: int,
    total_samples: int,
    median_abs_r_delta: Decimal,
    p95_abs_r_delta: Decimal,
    lifecycle_agreement_rate: Decimal,
    tolerance: ReconciliationTolerance = DEFAULT_RECONCILIATION_TOLERANCE,
) -> str:
    """Return the fail-closed Day 11 paper-to-broker reconciliation status."""
    if provider_samples < 0 or total_samples < 0:
        raise ValueError("reconciliation_sample_count_invalid")
    if median_abs_r_delta < 0 or p95_abs_r_delta < 0:
        raise ValueError("reconciliation_r_delta_invalid")
    if not Decimal("0") <= lifecycle_agreement_rate <= Decimal("1"):
        raise ValueError("reconciliation_lifecycle_rate_invalid")
    if not tolerance.version.strip():
        raise ValueError("reconciliation_tolerance_version_required")
    if provider_samples < tolerance.min_provider_signals:
        return "WAITING_INSUFFICIENT_PROVIDER_RECONCILIATION_SAMPLES"
    if total_samples < tolerance.min_total_signals:
        return "WAITING_INSUFFICIENT_GLOBAL_RECONCILIATION_SAMPLES"
    if median_abs_r_delta > tolerance.max_median_abs_r_delta:
        return "WAITING_RECONCILIATION_MEDIAN_R_DIVERGENCE"
    if p95_abs_r_delta > tolerance.max_p95_abs_r_delta:
        return "WAITING_RECONCILIATION_P95_R_DIVERGENCE"
    if lifecycle_agreement_rate < tolerance.min_lifecycle_agreement_rate:
        return "WAITING_RECONCILIATION_LIFECYCLE_DIVERGENCE"
    return "RECONCILED"


def intelligence_mode_for_calibration(status: str) -> str:
    """Later intelligence may leave WAITING only after explicit reconciliation."""
    return "RESEARCH_READY" if status == "RECONCILED" else "SHADOW_WAITING"


__all__ = [
    "CALIBRATION_TOLERANCE_VERSION",
    "DEFAULT_RECONCILIATION_TOLERANCE",
    "MIN_CALIBRATION_SAMPLES",
    "ReconciliationTolerance",
    "adverse_exit_slippage_points",
    "adverse_slippage_points",
    "calibration_status",
    "execution_adjusted_r",
    "intelligence_mode_for_calibration",
    "reconciliation_status",
]
