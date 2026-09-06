"""Provider Intelligence Day 11 execution-cost calibration helpers.

These functions are deliberately pure research math. They do not call MetaAPI, mutate
positions, change sizing, or grant broker authority. The production calibration views use
the same sign convention: positive slippage means worse execution for the provider side.
"""

from __future__ import annotations

from decimal import Decimal

MIN_CALIBRATION_SAMPLES = 30


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
    """Return signed adverse slippage for an exit.

    For a BUY, selling below the intended exit is adverse. For a SELL, buying back above
    the intended exit is adverse.
    """
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
    """Apply modeled broker-layer costs to a spread-aware shadow R result.

    Shadow execution already uses executable bid/ask and therefore already pays spread.
    This adjustment only subtracts additional broker fill slippage and explicit cash
    charges, preventing spread from being charged twice.
    """
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


__all__ = [
    "MIN_CALIBRATION_SAMPLES",
    "adverse_exit_slippage_points",
    "adverse_slippage_points",
    "calibration_status",
    "execution_adjusted_r",
]
