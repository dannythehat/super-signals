"""Build 5: point-in-time failure attribution, UNKNOWN discipline and self-critique.

This layer teaches AIDY to inspect its own *prior resolved* shadow decisions before
making a new shadow judgment. It is deliberately research-only. The historical replay
loader only exposes feedback whose source signal and result-known timestamp both predate
the target signal, so the model never sees the target or future outcome.

The central purpose is behavioural calibration, not a new alpha source:
- identify whether AIDY's own earlier filtering/reduction helped or harmed;
- distinguish genuine UNKNOWN from generic caution;
- prevent "reduce" from becoming a vague default when there is no current-trade reason;
- preserve every existing live-money and management authority gate.
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

CONTEXT_VERSION = "aidy_failure_self_critique_v1"
_BUILD4_REPLAY_VERSION = "aidy_historical_time_machine_v7"
_BUILD4_INPUT_CONTRACT_VERSION = "aidy_historical_replay_input_v6"
_MIN_PROVIDER_SELF_SAMPLE = 5
_MIN_GLOBAL_SELF_SAMPLE = 10

_REPLAY_FEEDBACK_SQL = text(
    """
    SELECT c.source_id,
           c.signal_posted_at AS prior_signal_at,
           s.outcome_resolved_at AS prior_result_known_at,
           d.output_payload->>'shadow_action' AS shadow_action,
           d.output_payload->>'risk_multiplier' AS risk_multiplier,
           s.replay_delta_vs_taken_usd AS shadow_delta_usd,
           (c.source_id=:source_id) AS same_provider
    FROM aidy_historical_replay_scores s
    JOIN aidy_historical_replay_decisions d ON d.id=s.replay_decision_id
    JOIN aidy_historical_replay_cases c ON c.id=d.case_id
    WHERE d.replay_version=:replay_version
      AND c.input_contract_version=:input_contract_version
      AND c.signal_posted_at<:as_of
      AND s.outcome_resolved_at<:as_of
    ORDER BY s.outcome_resolved_at DESC,d.id DESC
    LIMIT 100
    """
)


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _feedback_summary(rows: list[dict[str, Any]], *, minimum_sample: int) -> dict[str, Any]:
    actions = Counter(str(row.get("shadow_action") or "unknown") for row in rows)
    deltas = [_decimal(row.get("shadow_delta_usd")) or Decimal("0") for row in rows]
    improved = sum(1 for value in deltas if value > 0)
    harmed = sum(1 for value in deltas if value < 0)
    unchanged = len(deltas) - improved - harmed

    reduction_rows = [
        (row, delta)
        for row, delta in zip(rows, deltas, strict=False)
        if str(row.get("shadow_action") or "") == "reduce"
    ]
    skip_rows = [
        (row, delta)
        for row, delta in zip(rows, deltas, strict=False)
        if str(row.get("shadow_action") or "")
        in {"hold", "reject", "need_more_evidence"}
    ]

    reduce_helpful = sum(1 for _, value in reduction_rows if value > 0)
    reduce_harmful = sum(1 for _, value in reduction_rows if value < 0)
    skip_helpful = sum(1 for _, value in skip_rows if value > 0)
    skip_harmful = sum(1 for _, value in skip_rows if value < 0)
    net_delta = sum(deltas, Decimal("0"))

    if len(rows) < minimum_sample:
        failure_mode = "insufficient_prior_self_feedback"
        guidance = "no_behavioural_adjustment"
        status = "insufficient_prior_self_feedback"
    elif reduce_harmful > reduce_helpful and net_delta < 0:
        failure_mode = "over_reduction_of_profitable_trades"
        guidance = "require_current_trade_specific_reason_before_reduce"
        status = "descriptive_prior_self_feedback"
    elif skip_harmful > skip_helpful and net_delta < 0:
        failure_mode = "over_filtering_of_profitable_trades"
        guidance = "do_not_use_unknown_or_reject_as_generic_caution"
        status = "descriptive_prior_self_feedback"
    elif improved > harmed and net_delta > 0:
        failure_mode = "prior_filtering_added_value"
        guidance = "retain_case_specific_filtering_without_generalising"
        status = "descriptive_prior_self_feedback"
    else:
        failure_mode = "mixed_or_neutral"
        guidance = "no_directional_adjustment"
        status = "descriptive_prior_self_feedback"

    evidence_as_of = None
    if rows:
        known = [row.get("prior_result_known_at") for row in rows if row.get("prior_result_known_at")]
        if known:
            evidence_as_of = max(known).isoformat() if hasattr(max(known), "isoformat") else str(max(known))

    return {
        "status": status,
        "sample_n": len(rows),
        "minimum_sample_n": minimum_sample,
        "improved_n": improved,
        "harmed_n": harmed,
        "unchanged_n": unchanged,
        "net_shadow_delta_usd": str(net_delta),
        "actions": dict(sorted(actions.items())),
        "reduce": {
            "sample_n": len(reduction_rows),
            "helpful_n": reduce_helpful,
            "harmful_n": reduce_harmful,
            "net_shadow_delta_usd": str(
                sum((value for _, value in reduction_rows), Decimal("0"))
            ),
        },
        "skip": {
            "sample_n": len(skip_rows),
            "helpful_n": skip_helpful,
            "harmful_n": skip_harmful,
            "net_shadow_delta_usd": str(
                sum((value for _, value in skip_rows), Decimal("0"))
            ),
        },
        "dominant_failure_mode": failure_mode,
        "guidance": guidance,
        "evidence_as_of_utc": evidence_as_of,
        "descriptive_only": True,
        "selection_bias_possible": True,
        "usable_for_live_edge_claim": False,
        "usable_for_live_authority": False,
    }


def load_replay_self_feedback(
    session_factory: sessionmaker[Session],
    *,
    signal_posted_at: Any,
    source_id: Any,
) -> dict[str, Any]:
    """Load only Build 4 replay feedback that was fully known before the target signal."""
    with session_factory() as session:
        rows = [
            dict(row)
            for row in session.execute(
                _REPLAY_FEEDBACK_SQL,
                {
                    "source_id": source_id,
                    "as_of": signal_posted_at,
                    "replay_version": _BUILD4_REPLAY_VERSION,
                    "input_contract_version": _BUILD4_INPUT_CONTRACT_VERSION,
                },
            ).mappings()
        ]

    provider_rows = [row for row in rows if bool(row.get("same_provider"))]
    return {
        "same_provider": _feedback_summary(
            provider_rows, minimum_sample=_MIN_PROVIDER_SELF_SAMPLE
        ),
        "global": _feedback_summary(rows, minimum_sample=_MIN_GLOBAL_SELF_SAMPLE),
        "pit_contract": {
            "prior_signal_before_target_required": True,
            "prior_result_known_before_target_required": True,
            "source_replay_version": _BUILD4_REPLAY_VERSION,
            "source_input_contract_version": _BUILD4_INPUT_CONTRACT_VERSION,
        },
        "research_only": True,
        "live_money_execution_allowed": False,
    }


def _live_calibration_summary(
    self_calibration: Mapping[str, Any] | None,
) -> dict[str, Any]:
    calibration = self_calibration if isinstance(self_calibration, Mapping) else {}
    resolved = 0
    wins = 0
    losses = 0
    total_pnl = Decimal("0")
    by_lean: dict[str, Any] = {}
    for lean, raw in calibration.items():
        if not isinstance(raw, Mapping):
            continue
        n = int(raw.get("resolved") or 0)
        w = int(raw.get("wins") or 0)
        l = int(raw.get("losses") or 0)
        pnl = _decimal(raw.get("total_pnl_usd")) or Decimal("0")
        resolved += n
        wins += w
        losses += l
        total_pnl += pnl
        by_lean[str(lean)] = {
            "resolved": n,
            "wins": w,
            "losses": l,
            "total_pnl_usd": str(pnl),
        }
    return {
        "status": "descriptive_live_calibration" if resolved else "unavailable",
        "resolved": resolved,
        "wins": wins,
        "losses": losses,
        "total_pnl_usd": str(total_pnl),
        "by_lean": by_lean,
        "descriptive_only": True,
        "usable_for_live_edge_claim": False,
    }


def _evidence_gaps(
    *,
    market_context: Mapping[str, Any] | None,
    build2_context: Mapping[str, Any] | None,
    build3_context: Mapping[str, Any] | None,
    build4_context: Mapping[str, Any] | None,
) -> tuple[list[str], list[str]]:
    market = market_context if isinstance(market_context, Mapping) else {}
    build2 = build2_context if isinstance(build2_context, Mapping) else {}
    build3 = build3_context if isinstance(build3_context, Mapping) else {}
    build4 = build4_context if isinstance(build4_context, Mapping) else {}

    gaps: list[str] = []
    critical: list[str] = []

    geometry = (
        build4.get("signal_geometry")
        if isinstance(build4.get("signal_geometry"), Mapping)
        else {}
    )
    stop_distance = _decimal(geometry.get("stop_distance_points"))
    target_count = int(geometry.get("target_count") or 0)
    positive_target_count = int(geometry.get("positive_target_r_count") or 0)
    if stop_distance in (None, Decimal("0")):
        critical.append("invalid_or_missing_stop_distance")
    if target_count <= 0:
        critical.append("missing_take_profit_geometry")
    elif positive_target_count < target_count:
        critical.append("non_positive_target_geometry")

    liquidity = (
        build2.get("liquidity")
        if isinstance(build2.get("liquidity"), Mapping)
        else {}
    )
    if str(liquidity.get("quote_state") or "unknown").lower() != "known":
        gaps.append("quote_state_unknown")
    if str(liquidity.get("quote_freshness") or "unknown").lower() != "fresh":
        gaps.append("quote_not_fresh")

    trend = str(market.get("trend_structure") or "unknown").lower()
    if trend in {"", "none", "unknown"}:
        gaps.append("trend_structure_unknown")

    conditional = (
        build3.get("conditional_alpha")
        if isinstance(build3.get("conditional_alpha"), Mapping)
        else {}
    )
    if not bool(conditional.get("usable_as_pretrade_alpha")):
        gaps.append("conditional_alpha_unavailable")

    analogue = (
        build3.get("historical_analogue")
        if isinstance(build3.get("historical_analogue"), Mapping)
        else {}
    )
    if str(analogue.get("status") or "") != "descriptive_sample_available":
        gaps.append("historical_analogues_insufficient")

    probability = (
        build4.get("probability")
        if isinstance(build4.get("probability"), Mapping)
        else {}
    )
    if str(probability.get("status") or "") != "descriptive_low_sample":
        gaps.append("probability_sample_insufficient")

    execution = (
        build4.get("execution_cost_proxy")
        if isinstance(build4.get("execution_cost_proxy"), Mapping)
        else {}
    )
    if str(execution.get("status") or "") != "engineering_calibrated_proxy":
        gaps.append("execution_cost_unknown")

    return sorted(set(gaps)), sorted(set(critical))


def build_failure_self_critique_context(
    *,
    market_context: Mapping[str, Any] | None,
    build2_context: Mapping[str, Any] | None,
    build3_context: Mapping[str, Any] | None,
    build4_context: Mapping[str, Any] | None,
    self_calibration: Mapping[str, Any] | None = None,
    replay_self_feedback: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    gaps, critical = _evidence_gaps(
        market_context=market_context,
        build2_context=build2_context,
        build3_context=build3_context,
        build4_context=build4_context,
    )
    feedback = (
        replay_self_feedback
        if isinstance(replay_self_feedback, Mapping)
        else {}
    )
    provider_feedback = (
        feedback.get("same_provider")
        if isinstance(feedback.get("same_provider"), Mapping)
        else {}
    )
    global_feedback = (
        feedback.get("global")
        if isinstance(feedback.get("global"), Mapping)
        else {}
    )

    if provider_feedback.get("status") == "descriptive_prior_self_feedback":
        selected_scope = "same_provider"
        selected_feedback = dict(provider_feedback)
    elif global_feedback.get("status") == "descriptive_prior_self_feedback":
        selected_scope = "global"
        selected_feedback = dict(global_feedback)
    else:
        selected_scope = "none"
        selected_feedback = {
            "status": "insufficient_prior_self_feedback",
            "sample_n": 0,
            "dominant_failure_mode": "insufficient_prior_self_feedback",
            "guidance": "no_behavioural_adjustment",
            "descriptive_only": True,
        }

    if critical:
        unknown_status = "unknown_required"
    elif len(gaps) >= 4:
        unknown_status = "unknown_permitted"
    else:
        unknown_status = "evidence_usable"

    mode = str(selected_feedback.get("dominant_failure_mode") or "")
    if mode == "over_reduction_of_profitable_trades":
        adjustment_guard = "require_current_trade_specific_reason_before_reduce"
    elif mode == "over_filtering_of_profitable_trades":
        adjustment_guard = "do_not_use_unknown_or_reject_as_generic_caution"
    else:
        adjustment_guard = "no_prior_failure_adjustment"

    return {
        "contract_version": CONTEXT_VERSION,
        "failure_attribution": {
            "selected_feedback_scope": selected_scope,
            "prior_self_feedback": selected_feedback,
            "live_reasoning_calibration": _live_calibration_summary(self_calibration),
            "descriptive_only": True,
            "selection_bias_possible": True,
            "usable_for_live_edge_claim": False,
        },
        "unknown_gate": {
            "status": unknown_status,
            "critical_reasons": critical,
            "evidence_gaps": gaps,
            "gap_count": len(gaps),
            "unknown_is_not_default_caution": True,
            "need_more_evidence_requires_material_missing_evidence": True,
        },
        "risk_adjustment_guard": {
            "status": adjustment_guard,
            "reduce_requires_current_trade_specific_reason": True,
            "uncertainty_alone_is_not_a_reduce_reason": True,
            "prior_self_feedback_cannot_override_current_hard_evidence": True,
        },
        "self_critique_rules": [
            "separate_current_trade_evidence_from_prior_model_behaviour",
            "name_unknown_when_material_evidence_is_missing",
            "do_not_convert_descriptive_history_into_directional_edge",
            "do_not_reduce_as_a_generic_expression_of_caution",
            "prefer_no_behavioural_adjustment_when_prior_self_sample_is_insufficient",
        ],
        "research_only": True,
        "live_money_execution_allowed": False,
        "live_management_allowed": False,
    }


__all__ = [
    "CONTEXT_VERSION",
    "build_failure_self_critique_context",
    "load_replay_self_feedback",
]
