"""Provider Intelligence Day 20: active-management counterfactuals and safety revoke gates.

This module studies whether AIDY should have protected, reduced or closed a placed trade.
It cannot mutate MetaAPI/MT5 and cannot alter live or paper management. Efficacy remains
WAITING until substantial genuine forward OOS evidence exists.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone

MODEL_VERSION = "provider_day20_v1"
MANAGEMENT_EFFICACY = "WAITING-FOR-FORWARD-EVIDENCE"
LIVE_MANAGEMENT_ALLOWED = False
PAPER_MANAGEMENT_ALLOWED = False
GLOBAL_REVOKE_SUPPORTED = True


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timezone_aware_datetime_required")
    return value.astimezone(timezone.utc)


def _digest(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class ManagementDecision:
    signal_id: str
    decided_at: datetime
    action: str
    reason: str
    reduce_fraction: float | None = None
    protected_stop_r: float | None = None

    def validate(self) -> None:
        _utc(self.decided_at)
        if self.action not in {"hold", "protect", "reduce", "early_close"}:
            raise ValueError("unsupported_management_action")
        if self.action == "reduce":
            if self.reduce_fraction is None or not 0.0 < self.reduce_fraction < 1.0:
                raise ValueError("invalid_reduce_fraction")
        elif self.reduce_fraction is not None:
            raise ValueError("reduce_fraction_only_for_reduce")
        if self.action == "protect":
            if self.protected_stop_r is None or not math.isfinite(self.protected_stop_r):
                raise ValueError("invalid_protected_stop_r")
        elif self.protected_stop_r is not None:
            raise ValueError("protected_stop_only_for_protect")

    @property
    def executable(self) -> bool:
        return False

    @property
    def live_money_execution_allowed(self) -> bool:
        return False

    @property
    def decision_digest(self) -> str:
        self.validate()
        return _digest(
            {
                "model_version": MODEL_VERSION,
                "signal_id": self.signal_id,
                "decided_at": _utc(self.decided_at).isoformat(),
                "action": self.action,
                "reason": self.reason,
                "reduce_fraction": self.reduce_fraction,
                "protected_stop_r": self.protected_stop_r,
            }
        )


def resolve_management_counterfactual(
    *,
    decision: ManagementDecision,
    untouched_provider_baseline_r: float,
    intervention_gross_r: float,
    intervention_execution_cost_r: float,
    resolved_at: datetime,
) -> dict[str, object]:
    """Compare AIDY intervention with the untouched provider baseline after costs."""
    decision.validate()
    resolved = _utc(resolved_at)
    if resolved < _utc(decision.decided_at):
        raise ValueError("resolution_before_decision_forbidden")
    numbers = (untouched_provider_baseline_r, intervention_gross_r, intervention_execution_cost_r)
    if any(not math.isfinite(value) for value in numbers):
        raise ValueError("counterfactual_r_must_be_finite")
    if intervention_execution_cost_r < 0:
        raise ValueError("execution_cost_r_must_be_nonnegative")

    intervention_net_r = intervention_gross_r - intervention_execution_cost_r
    delta_r = intervention_net_r - untouched_provider_baseline_r
    return {
        "model_version": MODEL_VERSION,
        "signal_id": decision.signal_id,
        "action": decision.action,
        "decision_digest": decision.decision_digest,
        "untouched_provider_baseline_r": round(untouched_provider_baseline_r, 10),
        "intervention_gross_r": round(intervention_gross_r, 10),
        "intervention_execution_cost_r": round(intervention_execution_cost_r, 10),
        "intervention_net_r": round(intervention_net_r, 10),
        "management_delta_r": round(delta_r, 10),
        "loss_saved_r": round(max(0.0, delta_r), 10)
        if untouched_provider_baseline_r < 0
        else 0.0,
        "winner_sacrificed_r": round(max(0.0, -delta_r), 10)
        if untouched_provider_baseline_r > 0
        else 0.0,
        "resolved_at": resolved.isoformat(),
        "research_only": True,
        "live_money_execution_allowed": False,
        "management_efficacy": MANAGEMENT_EFFICACY,
    }


@dataclass(frozen=True, slots=True)
class SafetyState:
    global_aidy_revoked: bool = False
    kill_switch_active: bool = False
    stale_inputs: bool = False
    daily_loss_limit_breached: bool = False
    max_drawdown_limit_breached: bool = False
    consecutive_loss_limit_breached: bool = False
    paper_live_divergence: bool = False


def management_authority_guard(state: SafetyState) -> dict[str, object]:
    blockers = [
        name
        for name, active in (
            ("global_aidy_revoked", state.global_aidy_revoked),
            ("kill_switch_active", state.kill_switch_active),
            ("stale_inputs", state.stale_inputs),
            ("daily_loss_limit_breached", state.daily_loss_limit_breached),
            ("max_drawdown_limit_breached", state.max_drawdown_limit_breached),
            ("consecutive_loss_limit_breached", state.consecutive_loss_limit_breached),
            ("paper_live_divergence", state.paper_live_divergence),
        )
        if active
    ]
    return {
        "model_version": MODEL_VERSION,
        "blockers": blockers,
        "aidy_management_available": not bool(blockers),
        "auto_revert_to_provider_baseline": bool(blockers),
        "live_management_allowed": LIVE_MANAGEMENT_ALLOWED,
        "paper_management_allowed": PAPER_MANAGEMENT_ALLOWED,
        "global_revoke_supported": GLOBAL_REVOKE_SUPPORTED,
    }


def detect_paper_live_divergence(
    *,
    shadow_action: str,
    observed_live_action: str,
    shadow_geometry_digest: str,
    live_geometry_digest: str,
) -> dict[str, object]:
    divergence = (
        shadow_action != observed_live_action
        or shadow_geometry_digest != live_geometry_digest
    )
    return {
        "model_version": MODEL_VERSION,
        "divergence": divergence,
        "required_response": "AUTO_REVERT_SHADOW" if divergence else "NONE",
        "live_mutation_created": False,
    }


def management_effectiveness_report(rows: list[dict[str, object]]) -> dict[str, object]:
    delta = sum(float(row["management_delta_r"]) for row in rows)
    sacrificed = sum(float(row["winner_sacrificed_r"]) for row in rows)
    saved = sum(float(row["loss_saved_r"]) for row in rows)
    return {
        "model_version": MODEL_VERSION,
        "resolved_n": len(rows),
        "net_delta_r": round(delta, 10),
        "loss_saved_r": round(saved, 10),
        "winner_sacrificed_r": round(sacrificed, 10),
        "engineering_status": "GREEN",
        "management_efficacy": MANAGEMENT_EFFICACY,
        "live_management_allowed": LIVE_MANAGEMENT_ALLOWED,
        "paper_management_allowed": PAPER_MANAGEMENT_ALLOWED,
    }


def engineering_acceptance_snapshot() -> dict[str, object]:
    now = datetime(2026, 9, 8, 11, 0, tzinfo=timezone.utc)
    decision = ManagementDecision(
        signal_id="s1", decided_at=now, action="early_close", reason="research stress"
    )
    outcome = resolve_management_counterfactual(
        decision=decision,
        untouched_provider_baseline_r=-1.0,
        intervention_gross_r=-0.25,
        intervention_execution_cost_r=0.05,
        resolved_at=now,
    )
    guard = management_authority_guard(SafetyState(kill_switch_active=True))
    divergence = detect_paper_live_divergence(
        shadow_action="hold",
        observed_live_action="close",
        shadow_geometry_digest="a",
        live_geometry_digest="b",
    )
    return {
        "model_version": MODEL_VERSION,
        "engineering_harness_green": (
            outcome["management_delta_r"] == 0.7
            and guard["auto_revert_to_provider_baseline"] is True
            and divergence["required_response"] == "AUTO_REVERT_SHADOW"
            and decision.executable is False
        ),
        "management_efficacy": MANAGEMENT_EFFICACY,
        "live_management_allowed": LIVE_MANAGEMENT_ALLOWED,
        "global_revoke_supported": GLOBAL_REVOKE_SUPPORTED,
    }
