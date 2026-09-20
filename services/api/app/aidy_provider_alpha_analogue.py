"""Build 3: PIT-safe provider conditional-alpha readiness and historical analogues.

The existing Day 13 preregistered engine remains the single source of conditional-alpha
statistics. This module does not create a second hypothesis registry and does not promote
underpowered or post-entry-conditioned Day 13 cells into pre-trade alpha.

Historical analogues are descriptive research evidence only. A prior case is eligible only
when both its signal and its resolved outcome predate the target signal.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Mapping
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

CONTEXT_VERSION = "aidy_provider_alpha_analogue_v1"
ANALOGUE_VERSION = "aidy_historical_analogue_v1"
DAY13_REGISTRY_VERSION = "provider_day13_preregistered_v1"
DAY13_MODEL_VERSION = "provider_day13_v1"
_MIN_ANALOGUES_FOR_SUMMARY = 3
_MAX_ANALOGUES = 5

_DAY13_SESSION_MAP = {
    "asia": "asia",
    "london": "europe",
    "europe": "europe",
    "london_new_york_overlap": "ny_early",
    "ny_early": "ny_early",
    "new_york": "other",
    "rollover": "other",
    "other": "other",
}

_LOAD_DAY13 = text(
    """
    WITH latest AS (
        SELECT r.id,r.model_version,r.registry_version,r.evidence_cutoff,
               r.minimum_oos_n,r.proposed_fdr_q,r.proposed_min_effect_r,
               r.threshold_approval_status,r.statistical_status,
               r.engineering_status,r.eligible_oos_trade_count,
               r.tested_hypothesis_count,r.bh_rejected_count,
               r.builder_gate_candidate_count,r.authoritative_discovery_count
        FROM provider_conditional_runs r
        WHERE r.completed_at IS NOT NULL
          AND r.model_version=:model_version
          AND r.registry_version=:registry_version
          AND r.evidence_cutoff<=:as_of
        ORDER BY r.evidence_cutoff DESC,r.completed_at DESC,r.id DESC
        LIMIT 1
    )
    SELECT l.*,h.regime_dimension,h.regime_value,h.duration_bucket,h.duration_semantics,
           x.cell_oos_n,x.complement_oos_n,x.shrunken_effect_r,x.p_value,
           x.bh_adjusted_p,x.bh_rejected,x.minimum_oos_gate_met,
           x.minimum_effect_gate_met,x.builder_gate_candidate,
           x.authoritative_discovery,x.research_only,x.live_money_execution_allowed
    FROM latest l
    LEFT JOIN provider_conditional_results x ON x.run_id=l.id
    LEFT JOIN provider_conditional_hypotheses h ON h.id=x.hypothesis_id
    WHERE h.source_id=:source_id
      AND h.side=:side
      AND h.session_bucket=:session_bucket
    ORDER BY h.regime_dimension,h.regime_value,h.duration_bucket
    """
)

_LOAD_ANALOGUES = text(
    """
    SELECT c.id AS case_id,c.source_id,c.signal_posted_at,c.input_payload,
           s.resolution,s.actual_pnl_usd,s.actual_realized_r,s.outcome_resolved_at
    FROM aidy_historical_replay_cases c
    JOIN aidy_historical_replay_decisions d
      ON d.case_id=c.id
     AND d.replay_version='aidy_historical_time_machine_v5'
    JOIN aidy_historical_replay_scores s
      ON s.replay_decision_id=d.id
    WHERE c.input_contract_version='aidy_historical_replay_input_v4'
      AND c.signal_posted_at<:as_of
      AND s.outcome_resolved_at<=:as_of
      AND c.input_payload->'signal'->>'side'=:side
    ORDER BY c.signal_posted_at DESC,c.id
    LIMIT 80
    """
)


def _day13_session(value: Any) -> str:
    return _DAY13_SESSION_MAP.get(str(value or "").strip().lower(), "other")


def _regime_labels(market_context: Mapping[str, Any] | None) -> dict[str, str]:
    market = market_context if isinstance(market_context, Mapping) else {}
    labels: dict[str, str] = {}
    for key in ("trend_structure", "volatility_band", "event_timing"):
        value = str(market.get(key) or "").strip().lower()
        if value and value not in {"unknown", "none"}:
            labels[key] = value

    freshness = str(market.get("quote_freshness") or "").strip().lower()
    market_blob = market.get("market") if isinstance(market.get("market"), Mapping) else {}
    quote = (
        market_blob.get("quote_context")
        if isinstance(market_blob.get("quote_context"), Mapping)
        else {}
    )
    if freshness == "stale":
        labels["quote_spread_condition"] = "stale_quote"
    elif freshness == "fresh":
        labels["quote_spread_condition"] = (
            "fresh_quote_spread_known"
            if quote.get("spread") is not None
            else "fresh_quote_spread_unknown"
        )
    return labels


def build_conditional_alpha_context(
    rows: list[dict[str, Any]],
    *,
    market_context: Mapping[str, Any] | None,
    as_of: datetime,
) -> dict[str, Any]:
    """Project the existing Day 13 engine into a pre-trade-safe readiness packet."""

    labels = _regime_labels(market_context)
    if not rows:
        return {
            "status": "no_point_in_time_day13_run",
            "statistical_status": "UNKNOWN",
            "threshold_approval_status": "UNKNOWN",
            "matched_cell_count": 0,
            "testable_matched_cell_count": 0,
            "builder_gate_candidate_count": 0,
            "authoritative_discovery_count": 0,
            "usable_as_pretrade_alpha": False,
            "reason": "no_day13_run_available_before_signal",
            "research_only": True,
            "live_money_execution_allowed": False,
        }

    evidence_cutoff = rows[0].get("evidence_cutoff")
    if isinstance(evidence_cutoff, datetime) and evidence_cutoff.astimezone(UTC) > as_of.astimezone(UTC):
        raise ValueError("provider_alpha_future_evidence")

    matched = [
        row
        for row in rows
        if str(row.get("regime_dimension") or "") in labels
        and str(row.get("regime_value") or "").lower()
        == labels[str(row.get("regime_dimension"))]
    ]
    testable = [row for row in matched if row.get("p_value") is not None]
    builder = [row for row in matched if bool(row.get("builder_gate_candidate"))]
    authoritative = [row for row in matched if bool(row.get("authoritative_discovery"))]
    max_cell_n = max((int(row.get("cell_oos_n") or 0) for row in matched), default=0)
    max_complement_n = max(
        (int(row.get("complement_oos_n") or 0) for row in matched), default=0
    )
    threshold_status = str(rows[0].get("threshold_approval_status") or "UNKNOWN")
    statistical_status = str(rows[0].get("statistical_status") or "UNKNOWN")
    minimum_n = int(rows[0].get("minimum_oos_n") or 0)

    # Day 13's registered duration bucket is explicitly realized/post-entry descriptive.
    # Therefore even a populated cell cannot silently become entry-time directional alpha.
    pretrade_usable = bool(
        authoritative
        and threshold_status != "PROPOSED_UNAPPROVED"
        and statistical_status not in {"WAITING-FOR-FORWARD-EVIDENCE", "UNKNOWN"}
        and all(
            str(row.get("duration_semantics") or "")
            != "realized_descriptive_post_entry"
            for row in authoritative
        )
    )
    reason = (
        "authoritative_pretrade_alpha_available"
        if pretrade_usable
        else (
            "waiting_for_forward_evidence"
            if not testable or max_cell_n < minimum_n
            else "day13_duration_condition_is_post_entry_or_thresholds_unapproved"
        )
    )
    return {
        "status": "available_but_non_actionable" if not pretrade_usable else "actionable",
        "model_version": str(rows[0].get("model_version") or DAY13_MODEL_VERSION),
        "registry_version": str(rows[0].get("registry_version") or DAY13_REGISTRY_VERSION),
        "evidence_cutoff_utc": (
            evidence_cutoff.astimezone(UTC).isoformat()
            if isinstance(evidence_cutoff, datetime)
            else None
        ),
        "engineering_status": str(rows[0].get("engineering_status") or "UNKNOWN"),
        "statistical_status": statistical_status,
        "threshold_approval_status": threshold_status,
        "minimum_oos_n": minimum_n,
        "matched_regime_labels": labels,
        "matched_cell_count": len(matched),
        "max_matched_cell_oos_n": max_cell_n,
        "max_matched_complement_oos_n": max_complement_n,
        "testable_matched_cell_count": len(testable),
        "builder_gate_candidate_count": len(builder),
        "authoritative_discovery_count": len(authoritative),
        "duration_semantics": "realized_descriptive_post_entry",
        "usable_as_pretrade_alpha": pretrade_usable,
        "reason": reason,
        "research_only": True,
        "live_money_execution_allowed": False,
    }


def _target_features(payload: Mapping[str, Any]) -> dict[str, Any]:
    market = payload.get("market_context") if isinstance(payload.get("market_context"), Mapping) else {}
    build2 = (
        payload.get("event_liquidity_execution_context")
        if isinstance(payload.get("event_liquidity_execution_context"), Mapping)
        else {}
    )
    execution = (
        build2.get("execution_geometry")
        if isinstance(build2.get("execution_geometry"), Mapping)
        else {}
    )
    return {
        "session": str(market.get("session") or "").lower() or None,
        "trend_structure": str(market.get("trend_structure") or "").lower() or None,
        "volatility_band": str(market.get("volatility_band") or "").lower() or None,
        "event_timing": str(market.get("event_timing") or "").lower() or None,
        "quote_freshness": str(market.get("quote_freshness") or "").lower() or None,
        "quote_state": str(market.get("quote_state") or "").lower() or None,
        "entry_zone_relation": execution.get("entry_zone_relation_to_mid"),
        "targets_crossed": execution.get("targets_already_crossed_at_quote"),
    }


def _similarity(
    target_payload: Mapping[str, Any],
    candidate_payload: Mapping[str, Any],
    *,
    same_provider: bool,
) -> tuple[float, list[str], int]:
    target = _target_features(target_payload)
    candidate = _target_features(candidate_payload)
    weights = {
        "session": 2,
        "trend_structure": 2,
        "volatility_band": 1,
        "event_timing": 1,
        "quote_freshness": 1,
        "quote_state": 1,
        "entry_zone_relation": 1,
        "targets_crossed": 1,
    }
    matched_weight = 2 if same_provider else 0
    comparable_weight = 2
    matched: list[str] = ["same_provider"] if same_provider else []
    for key, weight in weights.items():
        left = target.get(key)
        right = candidate.get(key)
        if left in (None, "", "unknown") or right in (None, "", "unknown"):
            continue
        comparable_weight += weight
        if left == right:
            matched_weight += weight
            matched.append(key)
    return matched_weight / comparable_weight, matched, comparable_weight


def build_historical_analogue_context(
    *,
    target_payload: Mapping[str, Any],
    target_source_id: UUID | str,
    target_signal_at: datetime,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Rank only already-resolved prior cases by deterministic pre-signal similarity."""

    target_at = target_signal_at.astimezone(UTC)
    ranked: list[dict[str, Any]] = []
    for row in rows:
        prior_signal = row.get("signal_posted_at")
        resolved_at = row.get("outcome_resolved_at")
        if not isinstance(prior_signal, datetime) or not isinstance(resolved_at, datetime):
            continue
        if prior_signal.astimezone(UTC) >= target_at or resolved_at.astimezone(UTC) > target_at:
            raise ValueError("historical_analogue_future_evidence")
        payload = row.get("input_payload")
        if not isinstance(payload, Mapping):
            continue
        same_provider = str(row.get("source_id")) == str(target_source_id)
        score, matched, comparable_weight = _similarity(
            target_payload, payload, same_provider=same_provider
        )
        if comparable_weight < 4 or score < 0.50:
            continue
        pnl = Decimal(str(row.get("actual_pnl_usd") or 0))
        r_value = row.get("actual_realized_r")
        ranked.append(
            {
                "case_id": str(row.get("case_id")),
                "signal_posted_at": prior_signal.astimezone(UTC).isoformat(),
                "prior_result_known_at": resolved_at.astimezone(UTC).isoformat(),
                "same_provider": same_provider,
                "similarity": round(score, 6),
                "matched_features": matched,
                "prior_pnl_usd": str(pnl),
                "prior_realized_r": str(r_value) if r_value is not None else None,
                "prior_result_class": str(row.get("resolution") or "unknown"),
            }
        )

    ranked.sort(
        key=lambda item: (
            -float(item["similarity"]),
            -int(bool(item["same_provider"])),
            item["signal_posted_at"],
            item["case_id"],
        )
    )
    top = ranked[:_MAX_ANALOGUES]
    enough = len(top) >= _MIN_ANALOGUES_FOR_SUMMARY
    pnl_values = [Decimal(item["prior_pnl_usd"]) for item in top]
    positive = sum(1 for value in pnl_values if value > 0)
    mean_pnl = (
        sum(pnl_values, Decimal("0")) / Decimal(len(pnl_values)) if pnl_values else None
    )
    return {
        "analogue_version": ANALOGUE_VERSION,
        "status": "descriptive_sample_available" if enough else "insufficient_prior_analogues",
        "sample_n": len(top),
        "minimum_sample_n": _MIN_ANALOGUES_FOR_SUMMARY,
        "positive_outcome_count": positive if enough else None,
        "positive_outcome_rate": (
            round(positive / len(top), 6) if enough and top else None
        ),
        "mean_actual_pnl_usd": str(mean_pnl) if enough and mean_pnl is not None else None,
        "analogues": top,
        "selection_bias_possible": True,
        "descriptive_only": True,
        "usable_for_live_edge_claim": False,
        "all_outcomes_resolved_before_target": True,
        "research_only": True,
        "live_money_execution_allowed": False,
    }


def load_provider_alpha_analogue_context(
    session_factory: sessionmaker[Session],
    *,
    signal_posted_at: datetime,
    source_id: UUID | str,
    side: str,
    market_context: Mapping[str, Any] | None,
    target_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Load a fully PIT-bounded Build 3 context for live or historical reasoning."""

    as_of = signal_posted_at.astimezone(UTC)
    day13_session = _day13_session((market_context or {}).get("session"))
    with session_factory() as session:
        alpha_rows = [
            dict(row)
            for row in session.execute(
                _LOAD_DAY13,
                {
                    "model_version": DAY13_MODEL_VERSION,
                    "registry_version": DAY13_REGISTRY_VERSION,
                    "as_of": as_of,
                    "source_id": str(source_id),
                    "side": str(side).upper(),
                    "session_bucket": day13_session,
                },
            ).mappings()
        ]
        analogue_rows = [
            dict(row)
            for row in session.execute(
                _LOAD_ANALOGUES,
                {"as_of": as_of, "side": str(side).upper()},
            ).mappings()
        ]

    return {
        "contract_version": CONTEXT_VERSION,
        "as_of_utc": as_of.isoformat(),
        "conditional_alpha": build_conditional_alpha_context(
            alpha_rows, market_context=market_context, as_of=as_of
        ),
        "historical_analogue": build_historical_analogue_context(
            target_payload=target_payload,
            target_source_id=source_id,
            target_signal_at=as_of,
            rows=analogue_rows,
        ),
        "research_only": True,
        "live_money_execution_allowed": False,
    }


__all__ = [
    "ANALOGUE_VERSION",
    "CONTEXT_VERSION",
    "build_conditional_alpha_context",
    "build_historical_analogue_context",
    "load_provider_alpha_analogue_context",
]
