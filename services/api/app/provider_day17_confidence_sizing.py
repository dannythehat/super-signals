"""Provider Intelligence Day 17 confidence calibration and dormant sizing harness.

This module deliberately has no execution-side effects. It can describe confidence
calibration, model a future 0.2%-2.0% research envelope, and calculate net/gross
XAUUSD heat. Current accepted paper/live trades remain on one caller-supplied flat
size while variable-sizing authority is WAITING-FOR-FORWARD-EVIDENCE.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

MODEL_VERSION = "provider_day17_v1"
VARIABLE_SIZING_AUTHORITY = "WAITING-FOR-FORWARD-EVIDENCE"
VARIABLE_SIZING_SCOPE = "DORMANT_RESEARCH_ONLY"
LIVE_VARIABLE_SIZING_ALLOWED = False
PAPER_VARIABLE_SIZING_ALLOWED = False
PROPOSED_ENVELOPE_MIN_RISK_FRACTION = 0.002
PROPOSED_ENVELOPE_MAX_RISK_FRACTION = 0.020
DEFAULT_RELIABILITY_BINS = 10
PROPOSED_MINIMUM_TOTAL_CALIBRATION_N = 100
PROPOSED_MINIMUM_NONEMPTY_BIN_N = 20
THRESHOLD_APPROVAL_STATUS = "PROPOSED_UNAPPROVED"


@dataclass(frozen=True, slots=True)
class CalibrationObservation:
    """One forward calibration observation.

    ``confidence`` is the confidence available at decision time and must be in [0, 1].
    ``realized_target`` is an explicitly defined binary calibration target (0 or 1).
    The caller owns the target definition; this harness never infers it from future data.
    """

    confidence: float
    realized_target: int

    def validate(self) -> None:
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence_out_of_range")
        if self.realized_target not in (0, 1):
            raise ValueError("realized_target_must_be_binary")


@dataclass(frozen=True, slots=True)
class HeatLeg:
    direction: str
    risk_fraction: float
    cluster_id: str

    def validate(self) -> None:
        if self.direction not in {"BUY", "SELL"}:
            raise ValueError("direction_must_be_BUY_or_SELL")
        if not math.isfinite(self.risk_fraction) or self.risk_fraction < 0:
            raise ValueError("risk_fraction_must_be_finite_nonnegative")
        if not self.cluster_id:
            raise ValueError("cluster_id_required")


@dataclass(frozen=True, slots=True)
class HeatCaps:
    net_cap_fraction: float
    gross_cap_fraction: float
    cluster_cap_fraction: float

    def validate(self) -> None:
        for name, value in (
            ("net_cap_fraction", self.net_cap_fraction),
            ("gross_cap_fraction", self.gross_cap_fraction),
            ("cluster_cap_fraction", self.cluster_cap_fraction),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name}_must_be_finite_positive")


def _wilson_interval(successes: int, n: int, *, z: float = 1.959963984540054) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    p = successes / n
    z2 = z * z
    denominator = 1.0 + z2 / n
    centre = (p + z2 / (2.0 * n)) / denominator
    margin = z * math.sqrt((p * (1.0 - p) + z2 / (4.0 * n)) / n) / denominator
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def reliability_table(
    observations: Iterable[CalibrationObservation],
    *,
    bins: int = DEFAULT_RELIABILITY_BINS,
) -> list[dict[str, float | int | bool]]:
    """Return deterministic equal-width reliability bins for a diagram/table."""
    if bins < 2 or bins > 100:
        raise ValueError("bins_out_of_range")

    materialized = list(observations)
    buckets: list[list[CalibrationObservation]] = [[] for _ in range(bins)]
    for observation in materialized:
        observation.validate()
        index = min(int(observation.confidence * bins), bins - 1)
        buckets[index].append(observation)

    rows: list[dict[str, float | int | bool]] = []
    for index, bucket in enumerate(buckets):
        lower = index / bins
        upper = (index + 1) / bins
        count = len(bucket)
        successes = sum(item.realized_target for item in bucket)
        mean_confidence = sum(item.confidence for item in bucket) / count if count else 0.0
        realized_rate = successes / count if count else 0.0
        ci_low, ci_high = _wilson_interval(successes, count)
        rows.append(
            {
                "bin_index": index,
                "confidence_lower": round(lower, 8),
                "confidence_upper": round(upper, 8),
                "n": count,
                "mean_confidence": round(mean_confidence, 8),
                "realized_rate": round(realized_rate, 8),
                "calibration_gap": round(abs(mean_confidence - realized_rate), 8) if count else 0.0,
                "realized_rate_ci95_low": round(ci_low, 8),
                "realized_rate_ci95_high": round(ci_high, 8),
                "minimum_bin_n_proposed_gate_met": count >= PROPOSED_MINIMUM_NONEMPTY_BIN_N if count else False,
            }
        )
    return rows


def calibration_report(
    observations: Iterable[CalibrationObservation],
    *,
    bins: int = DEFAULT_RELIABILITY_BINS,
) -> dict[str, object]:
    """Build a reliability/calibration report without granting sizing authority."""
    materialized = list(observations)
    for observation in materialized:
        observation.validate()

    table = reliability_table(materialized, bins=bins)
    n = len(materialized)
    if n:
        brier = sum((item.confidence - item.realized_target) ** 2 for item in materialized) / n
        ece = sum((row["n"] / n) * row["calibration_gap"] for row in table if row["n"])
        max_gap = max((float(row["calibration_gap"]) for row in table if row["n"]), default=0.0)
    else:
        brier = None
        ece = None
        max_gap = None

    nonempty_rows = [row for row in table if row["n"]]
    total_n_gate = n >= PROPOSED_MINIMUM_TOTAL_CALIBRATION_N
    nonempty_bin_n_gate = bool(nonempty_rows) and all(
        int(row["n"]) >= PROPOSED_MINIMUM_NONEMPTY_BIN_N for row in nonempty_rows
    )
    evidence_status = (
        "EVALUABLE-REQUIRES-PREREGISTERED-AUTHORITY-GATE"
        if total_n_gate and nonempty_bin_n_gate
        else "WAITING-FOR-FORWARD-EVIDENCE"
    )

    return {
        "model_version": MODEL_VERSION,
        "observation_n": n,
        "reliability_bins": table,
        "brier_score": None if brier is None else round(brier, 10),
        "expected_calibration_error": None if ece is None else round(ece, 10),
        "maximum_bin_gap": None if max_gap is None else round(max_gap, 10),
        "proposed_minimum_total_n": PROPOSED_MINIMUM_TOTAL_CALIBRATION_N,
        "proposed_minimum_nonempty_bin_n": PROPOSED_MINIMUM_NONEMPTY_BIN_N,
        "minimum_total_n_proposed_gate_met": total_n_gate,
        "minimum_nonempty_bin_n_proposed_gate_met": nonempty_bin_n_gate,
        "threshold_approval_status": THRESHOLD_APPROVAL_STATUS,
        "evidence_status": evidence_status,
        "variable_sizing_authority": VARIABLE_SIZING_AUTHORITY,
        "live_variable_sizing_allowed": LIVE_VARIABLE_SIZING_ALLOWED,
        "paper_variable_sizing_allowed": PAPER_VARIABLE_SIZING_ALLOWED,
    }


def proposed_envelope_risk_fraction(confidence: float) -> float:
    """Research-only future envelope mapping; never an execution decision."""
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence_out_of_range")
    width = PROPOSED_ENVELOPE_MAX_RISK_FRACTION - PROPOSED_ENVELOPE_MIN_RISK_FRACTION
    return round(PROPOSED_ENVELOPE_MIN_RISK_FRACTION + confidence * width, 10)


def current_execution_size(
    *,
    accepted: bool,
    flat_size_units: float,
    confidence: float | None,
    execution_mode: str,
) -> dict[str, object]:
    """Current legal sizing decision: rejected=0, accepted=flat, confidence ignored.

    This is intentionally fail-closed. There is no parameter that can enable variable
    sizing, so uncalibrated confidence cannot alter real or paper risk by construction.
    """
    if execution_mode not in {"live", "paper"}:
        raise ValueError("execution_mode_must_be_live_or_paper")
    if not math.isfinite(flat_size_units) or flat_size_units <= 0:
        raise ValueError("flat_size_units_must_be_finite_positive")
    if confidence is not None and (not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0):
        raise ValueError("confidence_out_of_range")

    size_units = flat_size_units if accepted else 0.0
    return {
        "model_version": MODEL_VERSION,
        "execution_mode": execution_mode,
        "accepted": accepted,
        "size_units": size_units,
        "sizing_method": "flat_accepted_size" if accepted else "rejected_zero_size",
        "confidence_used_for_size": False,
        "variable_sizing_authority": VARIABLE_SIZING_AUTHORITY,
        "reason": (
            "accepted_trade_flat_size_during_calibration_evidence_collection"
            if accepted
            else "rejected_or_skipped_trade_zero_size"
        ),
    }


def research_only_future_sizing_candidate(confidence: float) -> dict[str, object]:
    """Expose future envelope math as a non-executable research artifact."""
    return {
        "model_version": MODEL_VERSION,
        "confidence": confidence,
        "proposed_risk_fraction": proposed_envelope_risk_fraction(confidence),
        "minimum_risk_fraction": PROPOSED_ENVELOPE_MIN_RISK_FRACTION,
        "maximum_risk_fraction": PROPOSED_ENVELOPE_MAX_RISK_FRACTION,
        "executable": False,
        "scope": VARIABLE_SIZING_SCOPE,
        "authority": VARIABLE_SIZING_AUTHORITY,
    }


def summarize_heat(open_legs: Iterable[HeatLeg]) -> dict[str, object]:
    """Summarize gross, signed net and provider-cluster XAUUSD heat."""
    legs = list(open_legs)
    long_heat = 0.0
    short_heat = 0.0
    cluster_heat: dict[str, float] = {}
    for leg in legs:
        leg.validate()
        if leg.direction == "BUY":
            long_heat += leg.risk_fraction
        else:
            short_heat += leg.risk_fraction
        cluster_heat[leg.cluster_id] = cluster_heat.get(leg.cluster_id, 0.0) + leg.risk_fraction

    signed_net = long_heat - short_heat
    gross = long_heat + short_heat
    return {
        "long_heat_fraction": round(long_heat, 10),
        "short_heat_fraction": round(short_heat, 10),
        "signed_net_heat_fraction": round(signed_net, 10),
        "absolute_net_heat_fraction": round(abs(signed_net), 10),
        "gross_heat_fraction": round(gross, 10),
        "cluster_heat_fraction": {key: round(value, 10) for key, value in sorted(cluster_heat.items())},
    }


def allocate_research_heat(
    *,
    open_legs: Iterable[HeatLeg],
    candidate: HeatLeg,
    caps: HeatCaps,
) -> dict[str, object]:
    """Compute the maximum research risk contribution allowed by explicit caps.

    This allocator is descriptive/dormant. It never places a trade or changes the
    current flat execution size. Net heat is direction-aware; gross and cluster heat
    still constrain an apparent hedge.
    """
    candidate.validate()
    caps.validate()
    summary = summarize_heat(open_legs)

    signed_net = float(summary["signed_net_heat_fraction"])
    gross = float(summary["gross_heat_fraction"])
    clusters = dict(summary["cluster_heat_fraction"])
    existing_cluster = float(clusters.get(candidate.cluster_id, 0.0))

    existing_violation = (
        abs(signed_net) > caps.net_cap_fraction + 1e-12
        or gross > caps.gross_cap_fraction + 1e-12
        or any(float(value) > caps.cluster_cap_fraction + 1e-12 for value in clusters.values())
    )
    if existing_violation:
        allowed = 0.0
        limiting = ["existing_heat_already_outside_caps"]
    else:
        gross_remaining = max(0.0, caps.gross_cap_fraction - gross)
        cluster_remaining = max(0.0, caps.cluster_cap_fraction - existing_cluster)
        if candidate.direction == "BUY":
            net_remaining = max(0.0, caps.net_cap_fraction - signed_net)
        else:
            net_remaining = max(0.0, caps.net_cap_fraction + signed_net)

        allowed = min(candidate.risk_fraction, gross_remaining, cluster_remaining, net_remaining)
        limiting = []
        tolerance = 1e-12
        if allowed + tolerance < candidate.risk_fraction:
            if abs(allowed - gross_remaining) <= tolerance:
                limiting.append("gross_heat_cap")
            if abs(allowed - cluster_remaining) <= tolerance:
                limiting.append("provider_cluster_heat_cap")
            if abs(allowed - net_remaining) <= tolerance:
                limiting.append("net_directional_heat_cap")
        if not limiting:
            limiting.append("requested_risk_fits_caps")

    proposed = HeatLeg(candidate.direction, max(0.0, allowed), candidate.cluster_id)
    post_summary = summarize_heat([*list(open_legs), proposed])
    return {
        "model_version": MODEL_VERSION,
        "research_only": True,
        "executable": False,
        "requested_risk_fraction": round(candidate.risk_fraction, 10),
        "allowed_risk_fraction": round(max(0.0, allowed), 10),
        "limiting_reasons": limiting,
        "pre_heat": summary,
        "post_heat_if_research_allocation_applied": post_summary,
        "caps": {
            "net_cap_fraction": caps.net_cap_fraction,
            "gross_cap_fraction": caps.gross_cap_fraction,
            "cluster_cap_fraction": caps.cluster_cap_fraction,
        },
        "variable_sizing_authority": VARIABLE_SIZING_AUTHORITY,
    }


def engineering_acceptance_snapshot() -> dict[str, object]:
    """Machine-readable Day 17 engineering status; not statistical authority."""
    low = current_execution_size(
        accepted=True,
        flat_size_units=1.0,
        confidence=0.05,
        execution_mode="paper",
    )
    high = current_execution_size(
        accepted=True,
        flat_size_units=1.0,
        confidence=0.95,
        execution_mode="paper",
    )
    return {
        "model_version": MODEL_VERSION,
        "engineering_harness_green": low["size_units"] == high["size_units"] == 1.0,
        "confidence_changes_current_paper_size": low["size_units"] != high["size_units"],
        "variable_sizing_authority": VARIABLE_SIZING_AUTHORITY,
        "live_variable_sizing_allowed": LIVE_VARIABLE_SIZING_ALLOWED,
        "paper_variable_sizing_allowed": PAPER_VARIABLE_SIZING_ALLOWED,
        "research_envelope": [
            PROPOSED_ENVELOPE_MIN_RISK_FRACTION,
            PROPOSED_ENVELOPE_MAX_RISK_FRACTION,
        ],
    }
