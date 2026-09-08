"""Provider Intelligence Day 19: user-safe explanations and research resource budgets.

Skipped trades are internal-only.  Public explanation helpers intentionally accept no
provider-name argument, so private source identity cannot accidentally leak through this
surface. Resource limits throttle research enrichment only; they never mutate broker
execution or grant intelligence authority.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

MODEL_VERSION = "provider_day19_v1"
PRIVATE_PROVIDER_LABEL = "private signal source"
SKIPPED_TRADE_BROADCAST_ALLOWED = False
LIVE_EXECUTION_AFFECTED_BY_RESEARCH_BUDGET = False


@dataclass(frozen=True, slots=True)
class EvidenceState:
    forward_n: int
    statistical_status: str
    confidence: float | None = None

    def validate(self) -> None:
        if self.forward_n < 0:
            raise ValueError("forward_n_must_be_nonnegative")
        if self.confidence is not None and (
            not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0
        ):
            raise ValueError("confidence_out_of_range")


def evidence_language(state: EvidenceState) -> str:
    state.validate()
    if state.statistical_status != "GREEN":
        return f"early forward evidence N={state.forward_n}; not statistically validated"
    if state.confidence is None:
        return f"statistically validated forward evidence N={state.forward_n}"
    return (
        f"statistically validated forward evidence N={state.forward_n}; "
        f"calibrated confidence {state.confidence:.0%}"
    )


def placed_trade_explanation(
    *,
    placed: bool,
    direction: str,
    market_context: str,
    evidence: EvidenceState,
    risk_note: str,
) -> dict[str, object]:
    """Create a public-safe explanation only for a trade that was actually placed."""
    if direction not in {"BUY", "SELL"}:
        raise ValueError("direction_must_be_BUY_or_SELL")
    if not placed:
        return {
            "model_version": MODEL_VERSION,
            "broadcast_allowed": False,
            "text": None,
            "reason": "skipped_trade_internal_only",
        }
    language = evidence_language(evidence)
    text = (
        f"{direction} XAUUSD placed. AIDY context: {market_context}. "
        f"Evidence: {language}. Risk: {risk_note}."
    )
    return {
        "model_version": MODEL_VERSION,
        "broadcast_allowed": True,
        "text": text,
        "provider_label": PRIVATE_PROVIDER_LABEL,
        "private_provider_name_included": False,
    }


def management_explanation(
    *,
    trade_was_placed: bool,
    action: str,
    reason: str,
    evidence: EvidenceState,
) -> dict[str, object]:
    if not trade_was_placed:
        return {"broadcast_allowed": False, "text": None, "reason": "no_placed_trade"}
    allowed = {"protect", "reduce", "move_stop", "close"}
    if action not in allowed:
        raise ValueError("unsupported_management_action")
    return {
        "broadcast_allowed": True,
        "text": f"Trade update: {action.replace('_', ' ')}. {reason}. Evidence: {evidence_language(evidence)}.",
        "provider_label": PRIVATE_PROVIDER_LABEL,
        "private_provider_name_included": False,
    }


def close_explanation(
    *,
    trade_was_placed: bool,
    pnl_r: float,
    close_reason: str,
    evidence: EvidenceState,
) -> dict[str, object]:
    if not trade_was_placed:
        return {"broadcast_allowed": False, "text": None, "reason": "no_placed_trade"}
    if not math.isfinite(pnl_r):
        raise ValueError("pnl_r_must_be_finite")
    outcome = "profit" if pnl_r > 0 else "loss" if pnl_r < 0 else "flat"
    return {
        "broadcast_allowed": True,
        "text": (
            f"Trade closed: {outcome} {pnl_r:+.2f}R. {close_reason}. "
            f"Evidence: {evidence_language(evidence)}."
        ),
        "provider_label": PRIVATE_PROVIDER_LABEL,
        "private_provider_name_included": False,
    }


@dataclass(frozen=True, slots=True)
class ResourceUsage:
    d1_reads: int = 0
    metaapi_calls: int = 0
    openai_calls: int = 0
    openai_input_tokens: int = 0
    openai_output_tokens: int = 0
    estimated_cost_usd: float = 0.0

    def validate(self) -> None:
        integer_values = (
            self.d1_reads,
            self.metaapi_calls,
            self.openai_calls,
            self.openai_input_tokens,
            self.openai_output_tokens,
        )
        if any(value < 0 for value in integer_values):
            raise ValueError("resource_usage_must_be_nonnegative")
        if not math.isfinite(self.estimated_cost_usd) or self.estimated_cost_usd < 0:
            raise ValueError("estimated_cost_must_be_finite_nonnegative")


@dataclass(frozen=True, slots=True)
class ResourceBudget:
    soft_d1_reads: int
    hard_d1_reads: int
    soft_metaapi_calls: int
    hard_metaapi_calls: int
    soft_openai_calls: int
    hard_openai_calls: int
    soft_cost_usd: float
    hard_cost_usd: float

    def validate(self) -> None:
        pairs = (
            (self.soft_d1_reads, self.hard_d1_reads),
            (self.soft_metaapi_calls, self.hard_metaapi_calls),
            (self.soft_openai_calls, self.hard_openai_calls),
        )
        if any(soft < 0 or hard <= 0 or soft > hard for soft, hard in pairs):
            raise ValueError("invalid_resource_budget")
        if (
            not math.isfinite(self.soft_cost_usd)
            or not math.isfinite(self.hard_cost_usd)
            or self.soft_cost_usd < 0
            or self.hard_cost_usd <= 0
            or self.soft_cost_usd > self.hard_cost_usd
        ):
            raise ValueError("invalid_resource_budget")


def evaluate_resource_budget(*, usage: ResourceUsage, budget: ResourceBudget) -> dict[str, object]:
    usage.validate()
    budget.validate()
    metrics = {
        "d1_reads": (usage.d1_reads, budget.soft_d1_reads, budget.hard_d1_reads),
        "metaapi_calls": (
            usage.metaapi_calls,
            budget.soft_metaapi_calls,
            budget.hard_metaapi_calls,
        ),
        "openai_calls": (usage.openai_calls, budget.soft_openai_calls, budget.hard_openai_calls),
        "estimated_cost_usd": (
            usage.estimated_cost_usd,
            budget.soft_cost_usd,
            budget.hard_cost_usd,
        ),
    }
    hard = [name for name, (value, _soft, hard_limit) in metrics.items() if value >= hard_limit]
    soft = [
        name
        for name, (value, soft_limit, hard_limit) in metrics.items()
        if value >= soft_limit and value < hard_limit
    ]
    if hard:
        status = "HARD_LIMIT"
    elif soft:
        status = "SOFT_ALERT"
    else:
        status = "WITHIN_BUDGET"
    return {
        "model_version": MODEL_VERSION,
        "status": status,
        "soft_alert_metrics": soft,
        "hard_limit_metrics": hard,
        "research_enrichment_allowed": not bool(hard),
        "live_execution_affected": LIVE_EXECUTION_AFFECTED_BY_RESEARCH_BUDGET,
        "usage": {
            "d1_reads": usage.d1_reads,
            "metaapi_calls": usage.metaapi_calls,
            "openai_calls": usage.openai_calls,
            "openai_input_tokens": usage.openai_input_tokens,
            "openai_output_tokens": usage.openai_output_tokens,
            "estimated_cost_usd": usage.estimated_cost_usd,
        },
    }


def engineering_acceptance_snapshot() -> dict[str, object]:
    tiny = EvidenceState(3, "WAITING-FOR-FORWARD-EVIDENCE", 0.95)
    skipped = placed_trade_explanation(
        placed=False,
        direction="BUY",
        market_context="bullish trend",
        evidence=tiny,
        risk_note="flat research size",
    )
    placed = placed_trade_explanation(
        placed=True,
        direction="BUY",
        market_context="bullish trend",
        evidence=tiny,
        risk_note="flat research size",
    )
    budget = evaluate_resource_budget(
        usage=ResourceUsage(d1_reads=100, metaapi_calls=2, openai_calls=2, estimated_cost_usd=1.0),
        budget=ResourceBudget(80, 120, 10, 20, 10, 20, 2.0, 3.0),
    )
    return {
        "model_version": MODEL_VERSION,
        "engineering_harness_green": (
            skipped["broadcast_allowed"] is False
            and skipped["text"] is None
            and placed["private_provider_name_included"] is False
            and "not statistically validated" in str(placed["text"])
            and budget["status"] == "SOFT_ALERT"
        ),
        "skipped_trade_broadcast_allowed": SKIPPED_TRADE_BROADCAST_ALLOWED,
        "private_provider_names_public": False,
        "live_execution_affected_by_research_budget": LIVE_EXECUTION_AFFECTED_BY_RESEARCH_BUDGET,
    }
