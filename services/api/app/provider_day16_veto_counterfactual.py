"""Provider Intelligence Day 16: PIT-safe AIDY veto/filter counterfactual harness.

The decision is made from evidence that existed before the provider signal.  The later
outcome is resolved separately.  Nothing in this module can suppress a live/paper trade
or publish a skipped trade to Telegram.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Iterable

MODEL_VERSION = "provider_day16_v1"
RULE_VERSION = "provider_day16_veto_preregistered_v1"
STATISTICAL_AUTHORITY = "WAITING-FOR-FORWARD-EVIDENCE"
LIVE_VETO_ALLOWED = False
PAPER_VETO_ALLOWED = False
SKIPPED_TRADE_TELEGRAM_BROADCAST_ALLOWED = False
# Inherited from the Day-13 preregistered conditional research constitution.
MINIMUM_OOS_N = 30
PROPOSED_MIN_NEGATIVE_EFFECT_R = 0.25


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timezone_aware_datetime_required")
    return value.astimezone(timezone.utc)


def _digest(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class PreregisteredWeakSetupEvidence:
    """One already-preregistered negative conditional result available at decision time."""

    hypothesis_id: str
    preregistered_at: datetime
    evidence_cutoff: datetime
    cell_oos_n: int
    complement_oos_n: int
    shrunken_effect_r: float
    bh_rejected: bool
    minimum_effect_gate_met: bool
    minimum_oos_gate_met: bool

    def validate_for(self, decision_at: datetime) -> None:
        decision = _utc(decision_at)
        if _utc(self.preregistered_at) > decision:
            raise ValueError("future_preregistration_forbidden")
        if _utc(self.evidence_cutoff) > decision:
            raise ValueError("future_evidence_forbidden")
        if self.cell_oos_n < 0 or self.complement_oos_n < 0:
            raise ValueError("negative_sample_size")
        if not math.isfinite(self.shrunken_effect_r):
            raise ValueError("nonfinite_effect")

    @property
    def research_reject_gate_met(self) -> bool:
        return (
            self.minimum_oos_gate_met
            and self.minimum_effect_gate_met
            and self.bh_rejected
            and self.cell_oos_n >= MINIMUM_OOS_N
            and self.complement_oos_n >= MINIMUM_OOS_N
            and self.shrunken_effect_r <= -PROPOSED_MIN_NEGATIVE_EFFECT_R
        )


@dataclass(frozen=True, slots=True)
class VetoDecision:
    signal_id: str
    source_id: str
    signal_posted_at: datetime
    decided_at: datetime
    research_action: str
    reason: str
    selected_hypothesis_id: str | None
    selected_shrunken_effect_r: float | None
    selected_cell_oos_n: int | None
    selected_complement_oos_n: int | None
    input_digest: str

    @property
    def telegram_broadcast_allowed(self) -> bool:
        # A skipped/rejected trade is always internal.  An accepted decision still does
        # not itself authorize a publication; normal placed-trade publishing owns that.
        return False

    @property
    def executable(self) -> bool:
        return False


def decide_veto_counterfactual(
    *,
    signal_id: str,
    source_id: str,
    signal_posted_at: datetime,
    decided_at: datetime,
    evidence: Iterable[PreregisteredWeakSetupEvidence],
) -> VetoDecision:
    """Choose a research accept/reject action from pre-decision evidence only."""
    posted = _utc(signal_posted_at)
    decided = _utc(decided_at)
    if decided < posted:
        raise ValueError("decision_before_signal_forbidden")

    candidates: list[PreregisteredWeakSetupEvidence] = []
    serialized: list[dict[str, object]] = []
    for item in evidence:
        item.validate_for(posted)
        serialized.append(
            {
                **asdict(item),
                "preregistered_at": _utc(item.preregistered_at).isoformat(),
                "evidence_cutoff": _utc(item.evidence_cutoff).isoformat(),
            }
        )
        if item.research_reject_gate_met:
            candidates.append(item)

    selected = min(
        candidates,
        key=lambda item: (item.shrunken_effect_r, -item.cell_oos_n, item.hypothesis_id),
        default=None,
    )
    if selected is None:
        action = "accept"
        reason = "no_preregistered_negative_oos_gate_met"
    else:
        action = "reject"
        reason = "preregistered_negative_oos_counterfactual"

    input_payload: dict[str, object] = {
        "model_version": MODEL_VERSION,
        "rule_version": RULE_VERSION,
        "signal_id": signal_id,
        "source_id": source_id,
        "signal_posted_at": posted.isoformat(),
        "evidence": sorted(serialized, key=lambda row: str(row["hypothesis_id"])),
    }
    return VetoDecision(
        signal_id=signal_id,
        source_id=source_id,
        signal_posted_at=posted,
        decided_at=decided,
        research_action=action,
        reason=reason,
        selected_hypothesis_id=None if selected is None else selected.hypothesis_id,
        selected_shrunken_effect_r=None if selected is None else selected.shrunken_effect_r,
        selected_cell_oos_n=None if selected is None else selected.cell_oos_n,
        selected_complement_oos_n=None if selected is None else selected.complement_oos_n,
        input_digest=_digest(input_payload),
    )


def resolve_counterfactual(
    *,
    decision: VetoDecision,
    execution_adjusted_baseline_r: float,
    resolved_at: datetime,
) -> dict[str, object]:
    """Resolve raw baseline vs research-filtered R after outcome truth exists."""
    if not math.isfinite(execution_adjusted_baseline_r):
        raise ValueError("baseline_r_must_be_finite")
    resolved = _utc(resolved_at)
    if resolved < decision.decided_at:
        raise ValueError("resolution_before_decision_forbidden")
    filtered_r = execution_adjusted_baseline_r if decision.research_action == "accept" else 0.0
    delta_r = filtered_r - execution_adjusted_baseline_r
    return {
        "model_version": MODEL_VERSION,
        "rule_version": RULE_VERSION,
        "signal_id": decision.signal_id,
        "research_action": decision.research_action,
        "baseline_execution_adjusted_r": round(execution_adjusted_baseline_r, 10),
        "filtered_execution_adjusted_r": round(filtered_r, 10),
        "filter_delta_r": round(delta_r, 10),
        "avoided_loss_r": round(max(0.0, -execution_adjusted_baseline_r), 10)
        if decision.research_action == "reject"
        else 0.0,
        "sacrificed_winner_r": round(max(0.0, execution_adjusted_baseline_r), 10)
        if decision.research_action == "reject"
        else 0.0,
        "resolved_at": resolved.isoformat(),
        "research_only": True,
        "live_money_execution_allowed": False,
        "statistical_authority": STATISTICAL_AUTHORITY,
    }


def veto_effectiveness_report(outcomes: Iterable[dict[str, object]]) -> dict[str, object]:
    rows = list(outcomes)
    baseline = sum(float(row["baseline_execution_adjusted_r"]) for row in rows)
    filtered = sum(float(row["filtered_execution_adjusted_r"]) for row in rows)
    rejected = [row for row in rows if row.get("research_action") == "reject"]
    return {
        "model_version": MODEL_VERSION,
        "resolved_n": len(rows),
        "rejected_n": len(rejected),
        "baseline_execution_adjusted_r": round(baseline, 10),
        "filtered_execution_adjusted_r": round(filtered, 10),
        "delta_r": round(filtered - baseline, 10),
        "avoided_loss_r": round(sum(float(row.get("avoided_loss_r", 0.0)) for row in rows), 10),
        "sacrificed_winner_r": round(
            sum(float(row.get("sacrificed_winner_r", 0.0)) for row in rows), 10
        ),
        "engineering_status": "GREEN",
        "statistical_status": STATISTICAL_AUTHORITY,
        "live_veto_allowed": LIVE_VETO_ALLOWED,
        "paper_veto_allowed": PAPER_VETO_ALLOWED,
    }


def engineering_acceptance_snapshot() -> dict[str, object]:
    now = datetime(2026, 9, 8, 10, 0, tzinfo=timezone.utc)
    weak = PreregisteredWeakSetupEvidence(
        hypothesis_id="h-weak",
        preregistered_at=now,
        evidence_cutoff=now,
        cell_oos_n=40,
        complement_oos_n=80,
        shrunken_effect_r=-0.40,
        bh_rejected=True,
        minimum_effect_gate_met=True,
        minimum_oos_gate_met=True,
    )
    decision = decide_veto_counterfactual(
        signal_id="s1",
        source_id="p1",
        signal_posted_at=now,
        decided_at=now,
        evidence=[weak],
    )
    outcome = resolve_counterfactual(
        decision=decision,
        execution_adjusted_baseline_r=-1.0,
        resolved_at=now,
    )
    return {
        "model_version": MODEL_VERSION,
        "engineering_harness_green": (
            decision.research_action == "reject"
            and outcome["filtered_execution_adjusted_r"] == 0.0
            and outcome["avoided_loss_r"] == 1.0
            and decision.telegram_broadcast_allowed is False
            and decision.executable is False
        ),
        "statistical_authority": STATISTICAL_AUTHORITY,
        "live_veto_allowed": LIVE_VETO_ALLOWED,
        "skipped_trade_telegram_broadcast_allowed": SKIPPED_TRADE_TELEGRAM_BROADCAST_ALLOWED,
    }
