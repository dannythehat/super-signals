"""Structured, point-in-time evidence contract for AIDY reasoning.

Provider history is useful only when AIDY can prove exactly which stored fact it used.
This module converts the provider profile/fingerprint/intelligence packet into atomic,
addressable claims and validates the model's claim references before an annotation can be
persisted. Missing evidence stays missing; no comparison is inferred from absent cohorts.
"""

from __future__ import annotations

import re
from typing import Any

EVIDENCE_CONTRACT_VERSION = "aidy_reasoning_evidence_v2"

# Free-form provider-history prose is deliberately prohibited. Provider history may affect
# the decision only through validated provider_claim_refs, whose exact facts are persisted.
_PROVIDER_HISTORY_PATTERNS = (
    re.compile(r"\bhistor(?:y|ic|ically)\b", re.I),
    re.compile(r"\btrack\s*record\b", re.I),
    re.compile(r"\bwin\s*rate\b", re.I),
    re.compile(r"\bweaker\s+side\b", re.I),
    re.compile(r"\bstronger\s+side\b", re.I),
    re.compile(r"\bbest\s+(?:side|session)\b", re.I),
    re.compile(r"\bworst\s+(?:side|session)\b", re.I),
    re.compile(r"\bprovider\s+(?:history|performance|record|profile)\b", re.I),
    re.compile(r"\btheir\s+(?:history|record|win\s*rate|performance)\b", re.I),
    re.compile(r"\b(?:buy|sell)\b.{0,36}\b(?:weak|strong|better|worse|outperform|underperform)", re.I),
    re.compile(r"\b(?:weak|strong|better|worse|outperform|underperform).{0,36}\b(?:buy|sell)\b", re.I),
    re.compile(r"\b(?:asia|london|new[ _-]?york|overlap|late)\b.{0,40}\b(?:weak|strong|better|worse|perform|prefer|favou?r)", re.I),
    re.compile(r"\b(?:prefer|prefers|preferred|favou?r|favou?rs|dominant).{0,40}\b(?:asia|london|new[ _-]?york|overlap|late|buy|sell)\b", re.I),
)


class EvidenceClaimValidationError(ValueError):
    """The model attempted to use provider evidence it was not actually given."""


def _claim(
    claim_id: str,
    *,
    kind: str,
    source: str,
    path: str,
    value: Any,
    sample_n: int | None,
    version: str | int | None,
    as_of_utc: str | None,
) -> dict[str, Any]:
    return {
        "id": claim_id,
        "kind": kind,
        "source": source,
        "path": path,
        "value": value,
        "sample_n": sample_n,
        "version": version,
        "as_of_utc": as_of_utc,
    }


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def build_provider_evidence_claims(
    *,
    provider_profile: dict[str, Any] | None,
    provider_intelligence: dict[str, Any] | None,
    provider_fingerprint: dict[str, Any] | None,
    signal_side: str | None = None,
    signal_session: str | None = None,
) -> list[dict[str, Any]]:
    """Return only provider facts that really exist in the PIT packet.

    Side/session claims are emitted only when that exact bucket has a positive sample.
    Therefore a profile containing SELL history but no BUY history can never create a
    "BUY is weaker" evidence claim by absence.
    """
    claims: list[dict[str, Any]] = []
    current_side = str(signal_side or "").upper().strip()
    current_session = str(signal_session or "").lower().strip().replace("-", "_").replace(" ", "_")
    profile = provider_profile or {}
    profile_version = profile.get("version_no")
    profile_as_of = profile.get("effective_at")
    performance = profile.get("performance")
    performance = performance if isinstance(performance, dict) else {}

    overall_n = _positive_int(performance.get("closed_outcomes"))
    if overall_n is not None:
        claims.append(
            _claim(
                "provider.performance.overall",
                kind="provider_performance",
                source="provider_profile",
                path="provider_profile.performance",
                value={
                    "closed_outcomes": overall_n,
                    "wins": performance.get("wins"),
                    "losses": performance.get("losses"),
                    "win_rate_percent": performance.get("win_rate_percent"),
                    "evidence_source": performance.get("evidence_source"),
                },
                sample_n=overall_n,
                version=profile_version,
                as_of_utc=profile_as_of,
            )
        )

    side_buckets = performance.get("side_buckets")
    if isinstance(side_buckets, dict) and current_side:
        for side, stats in sorted(side_buckets.items()):
            if not isinstance(stats, dict):
                continue
            side_key = str(side).upper()
            if side_key != current_side:
                continue
            n = _positive_int(stats.get("trades"))
            if n is None:
                continue
            claims.append(
                _claim(
                    f"provider.performance.side.{side_key}",
                    kind="provider_side_performance",
                    source="provider_profile",
                    path=f"provider_profile.performance.side_buckets.{side_key}",
                    value={
                        "trades": n,
                        "wins": stats.get("wins"),
                        "losses": stats.get("losses"),
                        "win_rate_percent": stats.get("win_rate_percent"),
                    },
                    sample_n=n,
                    version=profile_version,
                    as_of_utc=profile_as_of,
                )
            )

    session_buckets = performance.get("session_buckets_utc")
    if isinstance(session_buckets, dict) and current_session:
        for session, stats in sorted(session_buckets.items()):
            if not isinstance(stats, dict):
                continue
            session_key = str(session).lower().strip().replace("-", "_").replace(" ", "_")
            if session_key != current_session:
                continue
            n = _positive_int(stats.get("trades"))
            if n is None:
                continue
            claims.append(
                _claim(
                    f"provider.performance.session.{session_key}",
                    kind="provider_session_performance",
                    source="provider_profile",
                    path=f"provider_profile.performance.session_buckets_utc.{session_key}",
                    value={
                        "trades": n,
                        "wins": stats.get("wins"),
                        "losses": stats.get("losses"),
                        "win_rate_percent": stats.get("win_rate_percent"),
                    },
                    sample_n=n,
                    version=profile_version,
                    as_of_utc=profile_as_of,
                )
            )

    interpretation = profile.get("interpretation_context")
    interpretation = interpretation if isinstance(interpretation, dict) else {}
    for field in ("entry_style", "order_style", "management_style", "dominant_session_utc", "drift_status"):
        value = interpretation.get(field)
        if value in (None, "", "unknown"):
            continue
        claims.append(
            _claim(
                f"provider.behaviour.{field}",
                kind="provider_behaviour",
                source="provider_profile",
                path=f"provider_profile.interpretation_context.{field}",
                value=value,
                sample_n=_positive_int(profile.get("observed_messages")),
                version=profile_version,
                as_of_utc=profile_as_of,
            )
        )

    fp = provider_fingerprint or {}
    fp_version = fp.get("id")
    fp_as_of = fp.get("computed_at")
    fp_n = _positive_int(fp.get("trades_resolved"))
    if fp_n is not None:
        claims.append(
            _claim(
                "provider.fingerprint.overall",
                kind="provider_fingerprint",
                source="provider_trade_fingerprint",
                path="provider_fingerprint",
                value={
                    "trades_resolved": fp_n,
                    "wins": fp.get("wins"),
                    "losses": fp.get("losses"),
                    "win_rate_pct": fp.get("win_rate_pct"),
                    "trading_style": fp.get("trading_style"),
                },
                sample_n=fp_n,
                version=fp_version,
                as_of_utc=str(fp_as_of) if fp_as_of is not None else None,
            )
        )
    if fp.get("geometry_sample_met") is True:
        claims.append(
            _claim(
                "provider.fingerprint.geometry",
                kind="provider_geometry_history",
                source="provider_trade_fingerprint",
                path="provider_fingerprint.geometry",
                value={
                    "avg_stop_distance_won": fp.get("avg_stop_distance_won"),
                    "avg_stop_distance_lost": fp.get("avg_stop_distance_lost"),
                    "avg_planned_rr_won": fp.get("avg_planned_rr_won"),
                    "avg_planned_rr_lost": fp.get("avg_planned_rr_lost"),
                },
                sample_n=fp_n,
                version=fp_version,
                as_of_utc=str(fp_as_of) if fp_as_of is not None else None,
            )
        )
    if fp.get("side_sample_met") is True:
        for label in ("best", "worst"):
            side = fp.get(f"{label}_side")
            trades = _positive_int(fp.get(f"{label}_side_trades"))
            if side and trades and current_side and str(side).upper() == current_side:
                claims.append(
                    _claim(
                        f"provider.fingerprint.{label}_side",
                        kind="provider_side_comparison",
                        source="provider_trade_fingerprint",
                        path=f"provider_fingerprint.{label}_side",
                        value={
                            "side": side,
                            "trades": trades,
                            "win_rate_percent": fp.get(f"{label}_side_win_rate_pct"),
                        },
                        sample_n=trades,
                        version=fp_version,
                        as_of_utc=str(fp_as_of) if fp_as_of is not None else None,
                    )
                )
    if fp.get("session_sample_met") is True:
        for label in ("best", "worst"):
            session = fp.get(f"{label}_session")
            trades = _positive_int(fp.get(f"{label}_session_trades"))
            normalized_session = str(session or "").lower().strip().replace("-", "_").replace(" ", "_")
            if session and trades and current_session and normalized_session == current_session:
                claims.append(
                    _claim(
                        f"provider.fingerprint.{label}_session",
                        kind="provider_session_comparison",
                        source="provider_trade_fingerprint",
                        path=f"provider_fingerprint.{label}_session",
                        value={
                            "session": session,
                            "trades": trades,
                            "win_rate_percent": fp.get(f"{label}_session_win_rate_pct"),
                        },
                        sample_n=trades,
                        version=fp_version,
                        as_of_utc=str(fp_as_of) if fp_as_of is not None else None,
                    )
                )

    intel = provider_intelligence or {}
    governance = intel.get("governance")
    governance = governance if isinstance(governance, dict) else {}
    disposition = governance.get("research_disposition")
    if disposition:
        claims.append(
            _claim(
                "provider.governance.research_disposition",
                kind="provider_governance",
                source="provider_intelligence",
                path="provider_intelligence.governance.research_disposition",
                value={
                    "research_disposition": disposition,
                    "reasons": governance.get("reasons") or [],
                },
                sample_n=_positive_int(governance.get("closed_shadow_legs")),
                version=intel.get("contract_version"),
                as_of_utc=intel.get("evidence_as_of_utc"),
            )
        )

    return claims


def validate_provider_claim_refs(
    refs: list[str] | tuple[str, ...],
    claims: list[dict[str, Any]],
) -> tuple[str, ...]:
    allowed = {str(claim["id"]) for claim in claims}
    clean: list[str] = []
    for raw in refs:
        ref = str(raw)
        if ref not in allowed:
            raise EvidenceClaimValidationError(f"unsupported_provider_claim_ref:{ref}")
        if ref not in clean:
            clean.append(ref)
    return tuple(clean)


def assert_no_freeform_provider_history(
    *,
    provider_name: str,
    rationale: str,
    key_factors: list[str],
    action_reason: str,
) -> None:
    """Provider-history prose is not accepted outside validated claim refs.

    Market/session facts remain allowed. The block targets language that attributes a fact
    to the provider's past behaviour/performance. The provider's literal name is also
    blocked so a model cannot bypass the structured claim channel by paraphrasing.
    """
    text = " ".join([rationale, *key_factors, action_reason])
    provider = (provider_name or "").strip()
    if provider and provider.casefold() in text.casefold():
        raise EvidenceClaimValidationError("freeform_provider_name_claim")
    for pattern in _PROVIDER_HISTORY_PATTERNS:
        if pattern.search(text):
            raise EvidenceClaimValidationError(
                f"freeform_provider_history_claim:{pattern.pattern}"
            )


__all__ = [
    "EVIDENCE_CONTRACT_VERSION",
    "EvidenceClaimValidationError",
    "assert_no_freeform_provider_history",
    "build_provider_evidence_claims",
    "validate_provider_claim_refs",
]
