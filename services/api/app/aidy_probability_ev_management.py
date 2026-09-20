"""Build 4: point-in-time probability/EV and research-only profit-extraction context.

This module never sizes or manages a live/paper trade. It converts already-approved
provider evidence and signal geometry into transparent probability/EV diagnostics, and
surfaces the existing canonical profit-protection ladder as the untouched management
baseline. Day 17 confidence-sizing authority and Day 20 management authority remain
WAITING / non-executable.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from math import sqrt
from typing import Any, Mapping
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.provider_day17_confidence_sizing import (
    LIVE_VARIABLE_SIZING_ALLOWED,
    PAPER_VARIABLE_SIZING_ALLOWED,
    THRESHOLD_APPROVAL_STATUS as DAY17_THRESHOLD_APPROVAL_STATUS,
    VARIABLE_SIZING_AUTHORITY,
)
from app.provider_day20_management_counterfactual import (
    LIVE_MANAGEMENT_ALLOWED,
    MANAGEMENT_EFFICACY,
    PAPER_MANAGEMENT_ALLOWED,
)

CONTEXT_VERSION = "aidy_probability_ev_management_v1"
MIN_PROVIDER_COHORT_N = 30

_MANAGEMENT_EVIDENCE = text(
    """
    SELECT
      COUNT(DISTINCT d.id)::int AS decisions_n,
      COUNT(DISTINCT o.id)::int AS outcomes_n,
      AVG(o.management_delta_r)::numeric AS avg_management_delta_r,
      SUM(o.loss_saved_r)::numeric AS loss_saved_r,
      SUM(o.winner_sacrificed_r)::numeric AS winner_sacrificed_r,
      MAX(GREATEST(d.decided_at,COALESCE(o.resolved_at,d.decided_at))) AS evidence_as_of_utc
    FROM provider_management_counterfactual_decisions d
    LEFT JOIN provider_management_counterfactual_outcomes o
      ON o.decision_id=d.id
     AND o.resolved_at<=:as_of
    WHERE d.source_id=:source_id
      AND d.decided_at<=:as_of
    """
)


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _wilson(successes: int, n: int, *, z: float = 1.959963984540054) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    p = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2.0 * n)) / denom
    margin = z * sqrt((p * (1.0 - p) + z2 / (4.0 * n)) / n) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def _provider_probability_claim(
    claims: list[dict[str, Any]],
) -> dict[str, Any]:
    priorities = (
        "provider_side_performance",
        "provider_session_performance",
        "provider_performance",
    )
    selected: dict[str, Any] | None = None
    for kind in priorities:
        candidates = [
            claim
            for claim in claims
            if isinstance(claim, dict)
            and str(claim.get("kind") or "") == kind
            and int(claim.get("sample_n") or 0) >= MIN_PROVIDER_COHORT_N
        ]
        if candidates:
            selected = max(candidates, key=lambda item: int(item.get("sample_n") or 0))
            break

    if selected is None:
        return {
            "status": "insufficient_provider_probability_sample",
            "minimum_sample_n": MIN_PROVIDER_COHORT_N,
            "selected_claim_id": None,
            "sample_n": 0,
            "positive_outcome_probability": None,
            "ci95_low": None,
            "ci95_high": None,
            "calibrated_probability": False,
            "probability_semantics": "provider_win_rate_proxy",
        }

    value = selected.get("value") if isinstance(selected.get("value"), Mapping) else {}
    wins = int(value.get("wins") or 0)
    losses = int(value.get("losses") or 0)
    n = wins + losses
    if n < MIN_PROVIDER_COHORT_N:
        return {
            "status": "insufficient_provider_probability_sample",
            "minimum_sample_n": MIN_PROVIDER_COHORT_N,
            "selected_claim_id": str(selected.get("id") or ""),
            "sample_n": n,
            "positive_outcome_probability": None,
            "ci95_low": None,
            "ci95_high": None,
            "calibrated_probability": False,
            "probability_semantics": "provider_win_rate_proxy",
        }

    probability = wins / n
    low, high = _wilson(wins, n)
    return {
        "status": "provider_cohort_probability_available",
        "minimum_sample_n": MIN_PROVIDER_COHORT_N,
        "selected_claim_id": str(selected.get("id") or ""),
        "sample_n": n,
        "wins": wins,
        "losses": losses,
        "positive_outcome_probability": round(probability, 8),
        "ci95_low": round(low, 8),
        "ci95_high": round(high, 8),
        "claim_as_of_utc": str(selected.get("as_of_utc") or "") or None,
        "calibrated_probability": False,
        "probability_semantics": "provider_win_rate_proxy",
        "warning": "provider win rate is not a calibrated target-hit probability",
    }


def _analogue_probability(build3: Mapping[str, Any] | None) -> dict[str, Any]:
    root = build3 if isinstance(build3, Mapping) else {}
    analogue = (
        root.get("historical_analogue")
        if isinstance(root.get("historical_analogue"), Mapping)
        else {}
    )
    n = int(analogue.get("sample_n") or 0)
    positive = analogue.get("positive_outcome_count")
    if n < 3 or positive is None:
        return {
            "status": "insufficient_prior_analogues",
            "sample_n": n,
            "positive_outcome_probability": None,
            "calibrated_probability": False,
            "blended_into_primary_probability": False,
        }
    p = int(positive) / n
    low, high = _wilson(int(positive), n)
    return {
        "status": "descriptive_analogue_probability_available",
        "sample_n": n,
        "positive_outcome_probability": round(p, 8),
        "ci95_low": round(low, 8),
        "ci95_high": round(high, 8),
        "calibrated_probability": False,
        "blended_into_primary_probability": False,
        "selection_bias_possible": True,
    }


def _execution_friction_r(
    event_liquidity_execution_context: Mapping[str, Any] | None,
    *,
    risk_distance: Decimal,
) -> dict[str, Any]:
    root = event_liquidity_execution_context if isinstance(event_liquidity_execution_context, Mapping) else {}
    calibration = (
        root.get("broker_execution_calibration")
        if isinstance(root.get("broker_execution_calibration"), Mapping)
        else {}
    )
    if str(calibration.get("status") or "") != "engineering_calibrated":
        return {
            "status": "unknown",
            "p50_cost_r": None,
            "p95_cost_r": None,
            "cash_charge_r": None,
            "reason": "execution_calibration_not_engineering_calibrated",
        }
    entry_p50 = _decimal(calibration.get("entry_adverse_p50_points")) or Decimal("0")
    exit_p50 = _decimal(calibration.get("exit_adverse_p50_points")) or Decimal("0")
    entry_p95 = _decimal(calibration.get("entry_adverse_p95_points")) or Decimal("0")
    exit_p95 = _decimal(calibration.get("exit_adverse_p95_points")) or Decimal("0")
    if risk_distance <= 0:
        return {
            "status": "unknown",
            "p50_cost_r": None,
            "p95_cost_r": None,
            "cash_charge_r": None,
            "reason": "risk_distance_unavailable",
        }
    return {
        "status": "slippage_points_calibrated",
        "p50_cost_r": str((entry_p50 + exit_p50) / risk_distance),
        "p95_cost_r": str((entry_p95 + exit_p95) / risk_distance),
        "cash_charge_r": None,
        "reason": "cash charge excluded because lot/risk-dollar mapping is not part of this context",
    }


def _target_r(side: str, *, entry_mid: Decimal, stop: Decimal, target: Decimal) -> Decimal | None:
    risk = abs(entry_mid - stop)
    if risk <= 0:
        return None
    direction = side.upper()
    reward = target - entry_mid if direction == "BUY" else entry_mid - target
    if reward <= 0:
        return None
    return reward / risk


def _ev_rows(
    *,
    side: str,
    entry_low: Any,
    entry_high: Any,
    stop_loss: Any,
    take_profits: list[Any],
    probability: Mapping[str, Any],
    event_liquidity_execution_context: Mapping[str, Any] | None,
) -> dict[str, Any]:
    low = _decimal(entry_low)
    high = _decimal(entry_high)
    stop = _decimal(stop_loss)
    if low is None and high is None:
        return {"status": "geometry_unavailable", "targets": [], "allocation_weighted_ev_r": None}
    if low is not None and high is not None and low > high:
        low, high = high, low
    entry_mid = (
        (low + high) / Decimal("2")
        if low is not None and high is not None
        else (low if low is not None else high)
    )
    if entry_mid is None or stop is None or entry_mid == stop:
        return {"status": "geometry_unavailable", "targets": [], "allocation_weighted_ev_r": None}

    risk_distance = abs(entry_mid - stop)
    friction = _execution_friction_r(
        event_liquidity_execution_context, risk_distance=risk_distance
    )
    p = probability.get("positive_outcome_probability")
    p_low = probability.get("ci95_low")
    p_high = probability.get("ci95_high")
    targets: list[dict[str, Any]] = []
    for idx, raw in enumerate(take_profits, start=1):
        target = _decimal(raw)
        if target is None:
            continue
        reward_r = _target_r(side, entry_mid=entry_mid, stop=stop, target=target)
        if reward_r is None:
            targets.append(
                {
                    "tp_index": idx,
                    "target": str(target),
                    "status": "target_not_profitable_side_or_invalid_geometry",
                }
            )
            continue
        break_even_p = Decimal("1") / (Decimal("1") + reward_r)

        def ev(prob: Any, cost_key: str) -> str | None:
            if prob is None:
                return None
            prob_d = Decimal(str(prob))
            gross = prob_d * reward_r - (Decimal("1") - prob_d)
            cost_raw = friction.get(cost_key)
            cost = Decimal(str(cost_raw)) if cost_raw is not None else Decimal("0")
            return str(gross - cost)

        targets.append(
            {
                "tp_index": idx,
                "target": str(target),
                "reward_r": str(reward_r),
                "break_even_probability": str(break_even_p),
                "net_ev_r_mid_p50_cost": ev(p, "p50_cost_r"),
                "net_ev_r_ci_low_p50_cost": ev(p_low, "p50_cost_r"),
                "net_ev_r_ci_high_p95_cost": ev(p_high, "p95_cost_r"),
                "status": "proxy_ev_available" if p is not None else "probability_unavailable",
            }
        )

    return {
        "status": "per_target_proxy_ev_available" if p is not None else "probability_unavailable",
        "entry_mid": str(entry_mid),
        "risk_distance_points": str(risk_distance),
        "targets": targets,
        "execution_friction": friction,
        "allocation_weighted_ev_r": None,
        "allocation_weighted_ev_status": "UNKNOWN_NO_SAFE_TP_ALLOCATION_WEIGHTS",
        "warning": "EV uses provider win-rate proxy, not a calibrated probability of each TP hit",
    }


def _management_context(
    *,
    take_profits: list[Any],
    entry_low: Any,
    entry_high: Any,
    stop_loss: Any,
    side: str,
    evidence: Mapping[str, Any] | None,
) -> dict[str, Any]:
    evidence = evidence if isinstance(evidence, Mapping) else {}
    low = _decimal(entry_low)
    high = _decimal(entry_high)
    stop = _decimal(stop_loss)
    entry_mid = (
        (low + high) / Decimal("2")
        if low is not None and high is not None
        else (low if low is not None else high)
    )
    target_rs: list[Decimal | None] = []
    if entry_mid is not None and stop is not None and entry_mid != stop:
        for raw in take_profits:
            target = _decimal(raw)
            target_rs.append(
                _target_r(side, entry_mid=entry_mid, stop=stop, target=target)
                if target is not None
                else None
            )

    tp2_r = target_rs[1] if len(target_rs) >= 2 else None
    outcomes_n = int(evidence.get("outcomes_n") or 0)
    return {
        "management_evidence": {
            "decisions_n": int(evidence.get("decisions_n") or 0),
            "outcomes_n": outcomes_n,
            "avg_management_delta_r": (
                str(evidence.get("avg_management_delta_r"))
                if evidence.get("avg_management_delta_r") is not None
                else None
            ),
            "loss_saved_r": (
                str(evidence.get("loss_saved_r"))
                if evidence.get("loss_saved_r") is not None
                else None
            ),
            "winner_sacrificed_r": (
                str(evidence.get("winner_sacrificed_r"))
                if evidence.get("winner_sacrificed_r") is not None
                else None
            ),
            "management_efficacy": MANAGEMENT_EFFICACY,
        },
        "current_profit_protection_baseline": {
            "after_tp2": "cancel unused entries; move open TP3+ legs to their own entry",
            "remaining_leg_locked_floor_r_after_tp2": "0",
            "after_tp3": "move remaining TP4+/runner stop to TP2",
            "remaining_leg_locked_floor_r_after_tp3": str(tp2_r) if tp2_r is not None else None,
            "policy_source": "broker_settlement_canonical",
            "policy_changed_by_build4": False,
        },
        "research_counterfactuals": [
            {
                "action": "protect",
                "timing": "after_tp1",
                "description": "test earlier breakeven protection against untouched provider baseline",
                "executable": False,
            },
            {
                "action": "reduce",
                "timing": "after_tp1",
                "description": "test partial extraction only after preregistered fraction/efficacy evidence exists",
                "reduce_fraction": None,
                "executable": False,
            },
            {
                "action": "early_close",
                "timing": "before_terminal_outcome",
                "description": "test loss saved versus winner sacrificed after costs",
                "executable": False,
            },
        ],
        "day20_efficacy_ready": False,
        "live_management_allowed": LIVE_MANAGEMENT_ALLOWED,
        "paper_management_allowed": PAPER_MANAGEMENT_ALLOWED,
    }


def build_probability_ev_management_context(
    *,
    signal_posted_at: datetime,
    side: str,
    entry_low: Any,
    entry_high: Any,
    stop_loss: Any,
    take_profits: list[Any],
    provider_evidence_claims: list[dict[str, Any]],
    provider_alpha_analogue_context: Mapping[str, Any] | None,
    event_liquidity_execution_context: Mapping[str, Any] | None,
    management_evidence: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if signal_posted_at.tzinfo is None:
        raise ValueError("timezone_aware_signal_required")
    as_of = signal_posted_at.astimezone(UTC)

    management_as_of = None
    if isinstance(management_evidence, Mapping):
        raw = management_evidence.get("evidence_as_of_utc")
        if isinstance(raw, datetime):
            management_as_of = raw.astimezone(UTC)
        elif raw:
            management_as_of = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(UTC)
    if management_as_of is not None and management_as_of > as_of:
        raise ValueError("management_evidence_from_future")

    primary_probability = _provider_probability_claim(provider_evidence_claims)
    analogue_probability = _analogue_probability(provider_alpha_analogue_context)
    ev = _ev_rows(
        side=side,
        entry_low=entry_low,
        entry_high=entry_high,
        stop_loss=stop_loss,
        take_profits=take_profits,
        probability=primary_probability,
        event_liquidity_execution_context=event_liquidity_execution_context,
    )
    management = _management_context(
        take_profits=take_profits,
        entry_low=entry_low,
        entry_high=entry_high,
        stop_loss=stop_loss,
        side=side,
        evidence=management_evidence,
    )
    return {
        "contract_version": CONTEXT_VERSION,
        "as_of_utc": as_of.isoformat(),
        "probability": {
            "primary": primary_probability,
            "secondary_analogue": analogue_probability,
            "blend_status": "NOT_BLENDED_TO_AVOID_DOUBLE_COUNTING_CORRELATED_EVIDENCE",
            "confidence_score_is_probability": False,
        },
        "expected_value": ev,
        "profit_extraction_and_management": management,
        "authority": {
            "variable_sizing_authority": VARIABLE_SIZING_AUTHORITY,
            "day17_threshold_approval_status": DAY17_THRESHOLD_APPROVAL_STATUS,
            "live_variable_sizing_allowed": LIVE_VARIABLE_SIZING_ALLOWED,
            "paper_variable_sizing_allowed": PAPER_VARIABLE_SIZING_ALLOWED,
            "management_efficacy": MANAGEMENT_EFFICACY,
            "live_management_allowed": LIVE_MANAGEMENT_ALLOWED,
            "paper_management_allowed": PAPER_MANAGEMENT_ALLOWED,
        },
        "research_only": True,
        "live_money_execution_allowed": False,
    }


def load_management_evidence(
    session_factory: sessionmaker[Session],
    *,
    source_id: UUID | str,
    as_of: datetime,
) -> dict[str, Any]:
    if as_of.tzinfo is None:
        raise ValueError("timezone_aware_as_of_required")
    with session_factory() as session:
        row = session.execute(
            _MANAGEMENT_EVIDENCE,
            {"source_id": str(source_id), "as_of": as_of.astimezone(UTC)},
        ).mappings().one()
    return dict(row)


__all__ = [
    "CONTEXT_VERSION",
    "MIN_PROVIDER_COHORT_N",
    "build_probability_ev_management_context",
    "load_management_evidence",
]
