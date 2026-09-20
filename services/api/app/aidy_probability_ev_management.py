"""Build 4: conservative probability/EV and profit-extraction research context.

This layer is deliberately non-authoritative. Probability is estimated only from already-resolved
prior analogues supplied by Build 3. EV is a binary geometry proxy, not a claim that the trade will
win. Active AIDY management remains governed by the existing Day 20 counterfactual harness, whose
paper/live authority is still disabled while forward efficacy evidence is insufficient.
"""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from app.provider_day20_management_counterfactual import (
    LIVE_MANAGEMENT_ALLOWED,
    MANAGEMENT_EFFICACY,
    PAPER_MANAGEMENT_ALLOWED,
)

CONTEXT_VERSION = "aidy_probability_ev_management_v1"
_MIN_PROBABILITY_SAMPLE = 5
_Z_95 = 1.959963984540054


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _signal_geometry(signal: Mapping[str, Any]) -> dict[str, Any]:
    side = str(signal.get("side") or "").upper()
    low = _decimal(signal.get("entry_low"))
    high = _decimal(signal.get("entry_high"))
    stop = _decimal(signal.get("stop_loss"))
    if low is not None and high is not None and low > high:
        low, high = high, low
    entry = (
        (low + high) / Decimal("2")
        if low is not None and high is not None
        else (low if low is not None else high)
    )
    risk = abs(entry - stop) if entry is not None and stop is not None else None
    targets = [
        value
        for value in (_decimal(item) for item in (signal.get("take_profits") or []))
        if value is not None
    ]
    target_r: list[Decimal] = []
    if entry is not None and risk not in (None, Decimal("0")):
        for target in targets:
            reward = (target - entry) if side == "BUY" else (entry - target)
            if reward > 0:
                target_r.append(reward / risk)
    return {
        "side": side,
        "entry_mid": str(entry) if entry is not None else None,
        "stop_distance_points": str(risk) if risk is not None else None,
        "target_r_multiples": [str(value) for value in target_r],
        "first_target_r": str(target_r[0]) if target_r else None,
        "equal_weight_mean_target_r": (
            str(sum(target_r, Decimal("0")) / Decimal(len(target_r)))
            if target_r
            else None
        ),
        "target_count": len(targets),
        "positive_target_r_count": len(target_r),
        "allocation_assumption": "equal_weight_geometry_proxy_only",
    }


def _wilson_interval(positive: int, n: int) -> tuple[float, float] | None:
    if n <= 0:
        return None
    p = positive / n
    z2 = _Z_95 * _Z_95
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2.0 * n)) / denom
    half = (
        _Z_95
        * math.sqrt((p * (1.0 - p) / n) + (z2 / (4.0 * n * n)))
        / denom
    )
    return max(0.0, centre - half), min(1.0, centre + half)


def _probability_from_analogues(
    analogue_context: Mapping[str, Any] | None,
) -> dict[str, Any]:
    context = analogue_context if isinstance(analogue_context, Mapping) else {}
    rows = context.get("analogues")
    analogues = rows if isinstance(rows, list) else []
    realized_r: list[Decimal] = []
    for item in analogues:
        if not isinstance(item, Mapping):
            continue
        value = _decimal(item.get("prior_realized_r"))
        if value is not None:
            realized_r.append(value)
    n = len(realized_r)
    positive = sum(1 for value in realized_r if value > 0)
    negative = sum(1 for value in realized_r if value < 0)
    raw_rate = positive / n if n else None
    # Beta(1,1) posterior mean; used only as a conservative descriptive smoother.
    posterior = (positive + 1) / (n + 2) if n else None
    interval = _wilson_interval(positive, n)
    mean_r = (
        sum(realized_r, Decimal("0")) / Decimal(n)
        if n
        else None
    )
    sample_ready = n >= _MIN_PROBABILITY_SAMPLE
    return {
        "status": "descriptive_low_sample" if sample_ready else "insufficient_prior_outcomes",
        "sample_n": n,
        "minimum_sample_n": _MIN_PROBABILITY_SAMPLE,
        "positive_n": positive,
        "negative_n": negative,
        "raw_positive_rate": round(raw_rate, 6) if raw_rate is not None else None,
        "beta11_posterior_positive_probability": (
            round(posterior, 6) if posterior is not None else None
        ),
        "wilson95_low": round(interval[0], 6) if interval is not None else None,
        "wilson95_high": round(interval[1], 6) if interval is not None else None,
        "mean_prior_realized_r": str(mean_r) if mean_r is not None else None,
        "selection_bias_possible": True,
        "descriptive_only": True,
        "usable_for_live_edge_claim": False,
        "usable_for_entry_override": False,
    }


def _execution_cost_r(
    build2_context: Mapping[str, Any] | None,
    *,
    stop_distance_points: Decimal | None,
) -> dict[str, Any]:
    context = build2_context if isinstance(build2_context, Mapping) else {}
    calibration = (
        context.get("broker_execution_calibration")
        if isinstance(context.get("broker_execution_calibration"), Mapping)
        else {}
    )
    if (
        calibration.get("status") != "engineering_calibrated"
        or stop_distance_points in (None, Decimal("0"))
    ):
        return {
            "status": "unknown",
            "estimated_execution_cost_r_p50": None,
            "estimated_execution_cost_r_p95": None,
        }

    usd_per_point = _decimal(calibration.get("usd_per_point_per_lot_p50"))
    cash50 = _decimal(calibration.get("cash_charge_per_lot_p50_usd"))
    cash95 = _decimal(calibration.get("cash_charge_per_lot_p95_usd"))
    entry50 = _decimal(calibration.get("entry_adverse_p50_points"))
    entry95 = _decimal(calibration.get("entry_adverse_p95_points"))
    exit50 = _decimal(calibration.get("exit_adverse_p50_points"))
    exit95 = _decimal(calibration.get("exit_adverse_p95_points"))
    if usd_per_point in (None, Decimal("0")):
        return {
            "status": "unknown",
            "estimated_execution_cost_r_p50": None,
            "estimated_execution_cost_r_p95": None,
        }

    cash_points50 = (cash50 or Decimal("0")) / usd_per_point
    cash_points95 = (cash95 or Decimal("0")) / usd_per_point
    p50_points = (entry50 or Decimal("0")) + (exit50 or Decimal("0")) + cash_points50
    p95_points = (entry95 or Decimal("0")) + (exit95 or Decimal("0")) + cash_points95
    return {
        "status": "engineering_calibrated_proxy",
        "estimated_execution_cost_r_p50": str(p50_points / stop_distance_points),
        "estimated_execution_cost_r_p95": str(p95_points / stop_distance_points),
        "uses_demo_broker_calibration": True,
    }


def _ev_proxy(
    probability: Mapping[str, Any],
    geometry: Mapping[str, Any],
    costs: Mapping[str, Any],
) -> dict[str, Any]:
    """Keep empirical realised-R EV separate from target-hit break-even geometry.

    P(prior realised R > 0) is not P(current TP hit). Combining those two would create a
    false target EV. The only EV estimate here is the sample mean of prior resolved R,
    explicitly selection-biased and descriptive.
    """
    n = int(probability.get("sample_n") or 0)
    empirical_r = _decimal(probability.get("mean_prior_realized_r"))
    first_r = _decimal(geometry.get("first_target_r"))
    mean_target_r = _decimal(geometry.get("equal_weight_mean_target_r"))
    if n < _MIN_PROBABILITY_SAMPLE or empirical_r is None:
        return {
            "status": "insufficient_evidence",
            "analogue_empirical_ev_r": None,
            "first_target_break_even_probability": None,
            "mean_target_break_even_probability": None,
            "target_hit_probability_available": False,
            "geometry_binary_ev_computed": False,
            "current_execution_cost_r_p50": None,
        }

    return {
        "status": "descriptive_empirical_analogue_ev",
        "analogue_empirical_ev_r": str(empirical_r),
        "first_target_break_even_probability": (
            str(Decimal("1") / (Decimal("1") + first_r))
            if first_r is not None
            else None
        ),
        "mean_target_break_even_probability": (
            str(Decimal("1") / (Decimal("1") + mean_target_r))
            if mean_target_r is not None
            else None
        ),
        "target_hit_probability_available": False,
        "geometry_binary_ev_computed": False,
        "current_execution_cost_r_p50": (
            costs.get("estimated_execution_cost_r_p50")
            if costs.get("status") == "engineering_calibrated_proxy"
            else None
        ),
        "execution_cost_not_double_counted_into_prior_realized_r": True,
        "selection_bias_possible": True,
        "descriptive_only": True,
        "usable_for_live_edge_claim": False,
        "usable_for_entry_override": False,
    }


def _management_context(target_count: int) -> dict[str, Any]:
    ladder: list[dict[str, Any]] = []
    if target_count >= 3:
        ladder.append(
            {
                "trigger": "broker_confirmed_tp2_hit",
                "existing_canonical_action": "cancel_unused_entries_and_move_open_tp3_plus_to_own_entry",
            }
        )
    if target_count >= 4:
        ladder.append(
            {
                "trigger": "broker_confirmed_tp3_hit",
                "existing_canonical_action": "move_open_tp4_plus_or_runner_stop_to_signal_tp2",
            }
        )
    return {
        "day20_management_efficacy": MANAGEMENT_EFFICACY,
        "aidy_paper_management_allowed": PAPER_MANAGEMENT_ALLOWED,
        "aidy_live_management_allowed": LIVE_MANAGEMENT_ALLOWED,
        "research_management_posture": "provider_baseline_only_until_forward_efficacy_is_proven",
        "existing_canonical_profit_protection_ladder": ladder,
        "canonical_ladder_authority_owner": "broker_settlement_canonical",
        "aidy_must_not_duplicate_or_override_canonical_ladder": True,
    }


def build_probability_ev_management_context(
    *,
    signal: Mapping[str, Any],
    build2_context: Mapping[str, Any] | None,
    build3_context: Mapping[str, Any] | None,
) -> dict[str, Any]:
    geometry = _signal_geometry(signal)
    stop_distance = _decimal(geometry.get("stop_distance_points"))
    build3 = build3_context if isinstance(build3_context, Mapping) else {}
    analogue = (
        build3.get("historical_analogue")
        if isinstance(build3.get("historical_analogue"), Mapping)
        else {}
    )
    probability = _probability_from_analogues(analogue)
    costs = _execution_cost_r(build2_context, stop_distance_points=stop_distance)
    ev = _ev_proxy(probability, geometry, costs)
    return {
        "contract_version": CONTEXT_VERSION,
        "probability": probability,
        "signal_geometry": geometry,
        "execution_cost_proxy": costs,
        "expected_value": ev,
        "management_profit_extraction": _management_context(int(geometry["target_count"])),
        "research_only": True,
        "live_money_execution_allowed": False,
    }


__all__ = ["CONTEXT_VERSION", "build_probability_ev_management_context"]
