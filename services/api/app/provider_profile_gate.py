"""Hard Gate 0 for Provider Lab provider-specific understanding.

A provider may be observed and learned from before qualification, but performance
scoring must not start until an immutable point-in-time profile demonstrates that
we understand that provider's own signal grammar, timing, trade geometry and
management/message behaviour.

The gate is deliberately evidence-based rather than relying on the existing
``interpretation_readiness`` score.  Historical messages may teach the profile;
qualification only affects signals posted after that profile version was already
knowable, preserving the forward-only/no-hindsight boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from app.provider_profile_pit import ProviderProfilePIT

GATE_VERSION = "provider-profile-gate-v1"
MIN_ACCEPTED_SIGNALS = 3
MIN_ACTIVE_DAYS = 3
MIN_OBSERVED_MESSAGES = 30
MIN_PROVIDER_VOCABULARY = 3
MIN_NEW_TRADE_EXAMPLES = 1

_KNOWN_INTERPRETATION_FIELDS = (
    "entry_style",
    "order_style",
    "direction_style",
    "message_format",
    "message_sequence",
    "dominant_session_utc",
    "management_style",
)


def _dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _known(value: object) -> bool:
    return bool(str(value or "").strip()) and str(value).strip().lower() != "unknown"


@dataclass(frozen=True, slots=True)
class ProviderProfileGateDecision:
    gate_version: str
    qualified: bool
    blockers: tuple[str, ...]
    evidence: dict[str, Any]

    @property
    def state(self) -> str:
        return "qualified" if self.qualified else "profiling"


def evaluate_provider_profile_gate(
    profile: ProviderProfilePIT | None,
) -> ProviderProfileGateDecision:
    """Evaluate only the immutable provider profile available at signal time."""

    if profile is None:
        return ProviderProfileGateDecision(
            gate_version=GATE_VERSION,
            qualified=False,
            blockers=("provider_profile_pit_missing",),
            evidence={
                "profile_version_no": None,
                "profile_effective_at": None,
                "accepted_signals": 0,
                "active_days": 0,
                "observed_messages": 0,
            },
        )

    snapshot = _dict(profile.profile_snapshot)
    metadata = _dict(snapshot.get("profile_metadata"))
    adaptive = _dict(metadata.get("adaptive_v1"))
    language = _dict(adaptive.get("language"))
    footprint = _dict(metadata.get("footprint_v1"))
    interpretation = _dict(footprint.get("interpretation_context"))
    timing = _dict(footprint.get("timing_fingerprint"))
    behaviour = _dict(footprint.get("message_behaviour"))
    geometry = _dict(footprint.get("trade_geometry"))

    adaptive_signals = _int(language.get("accepted_signal_count"))
    footprint_signals = _int(geometry.get("accepted_signals"))
    accepted_signals = max(adaptive_signals, footprint_signals)
    active_days = _int(timing.get("active_days"))
    observed_messages = _int(behaviour.get("observed_messages"))

    vocabulary = interpretation.get("provider_vocabulary")
    vocabulary_count = len(vocabulary) if isinstance(vocabulary, list) else 0
    examples = _dict(language.get("grammar_examples_masked"))
    new_trade_examples = examples.get("new_trade")
    new_trade_example_count = len(new_trade_examples) if isinstance(new_trade_examples, list) else 0

    blockers: list[str] = []
    if not adaptive:
        blockers.append("adaptive_profile_missing")
    if not footprint:
        blockers.append("provider_footprint_missing")
    if accepted_signals < MIN_ACCEPTED_SIGNALS:
        blockers.append("insufficient_understood_signals")
    if active_days < MIN_ACTIVE_DAYS:
        blockers.append("insufficient_active_days")
    if observed_messages < MIN_OBSERVED_MESSAGES:
        blockers.append("insufficient_message_history")

    for field in _KNOWN_INTERPRETATION_FIELDS:
        if not _known(interpretation.get(field)):
            blockers.append(f"{field}_unknown")

    # A provider is not considered understood if we cannot describe the usual
    # stop/target geometry of already accepted signals. These are descriptive
    # research statistics only; historical prices are never reused as execution
    # evidence for a future signal.
    if geometry.get("median_stop_distance") is None:
        blockers.append("stop_geometry_unknown")
    if geometry.get("median_tp_count") is None:
        blockers.append("target_geometry_unknown")
    if vocabulary_count < MIN_PROVIDER_VOCABULARY:
        blockers.append("provider_vocabulary_insufficient")
    if new_trade_example_count < MIN_NEW_TRADE_EXAMPLES:
        blockers.append("new_trade_grammar_examples_missing")

    evidence = {
        "profile_version_no": profile.version_no,
        "profile_effective_at": profile.effective_at.isoformat(),
        "profile_fingerprint": profile.snapshot_fingerprint,
        "accepted_signals": accepted_signals,
        "adaptive_accepted_signals": adaptive_signals,
        "footprint_accepted_signals": footprint_signals,
        "active_days": active_days,
        "observed_messages": observed_messages,
        "provider_vocabulary_count": vocabulary_count,
        "new_trade_example_count": new_trade_example_count,
        "entry_style": interpretation.get("entry_style"),
        "order_style": interpretation.get("order_style"),
        "direction_style": interpretation.get("direction_style"),
        "message_format": interpretation.get("message_format"),
        "message_sequence": interpretation.get("message_sequence"),
        "dominant_session_utc": interpretation.get("dominant_session_utc"),
        "management_style": interpretation.get("management_style"),
        "median_stop_distance_known": geometry.get("median_stop_distance") is not None,
        "median_tp_count_known": geometry.get("median_tp_count") is not None,
    }
    return ProviderProfileGateDecision(
        gate_version=GATE_VERSION,
        qualified=not blockers,
        blockers=tuple(blockers),
        evidence=evidence,
    )


__all__ = [
    "GATE_VERSION",
    "MIN_ACCEPTED_SIGNALS",
    "MIN_ACTIVE_DAYS",
    "MIN_OBSERVED_MESSAGES",
    "ProviderProfileGateDecision",
    "evaluate_provider_profile_gate",
]
