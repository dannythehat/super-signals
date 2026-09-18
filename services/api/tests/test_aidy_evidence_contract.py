from __future__ import annotations

import pytest

from app.aidy_evidence_contract import (
    EvidenceClaimValidationError,
    assert_no_freeform_provider_history,
    build_provider_evidence_claims,
    validate_provider_claim_refs,
)


def _profile(*, side_buckets: dict) -> dict:
    return {
        "version_no": 633,
        "effective_at": "2026-09-18T14:44:00+00:00",
        "observed_messages": 500,
        "performance": {
            "closed_outcomes": sum(int(v.get("trades", 0)) for v in side_buckets.values()),
            "wins": sum(int(v.get("wins", 0)) for v in side_buckets.values()),
            "losses": sum(int(v.get("losses", 0)) for v in side_buckets.values()),
            "win_rate_percent": 50.0,
            "evidence_source": "broker",
            "side_buckets": side_buckets,
            "session_buckets_utc": {},
        },
        "interpretation_context": {
            "entry_style": "zone",
            "management_style": "active_management",
            "dominant_session_utc": "mixed",
        },
    }


def test_missing_buy_history_never_becomes_a_buy_claim() -> None:
    """Regression for the live Scalping error: SELL-only evidence cannot imply BUY weakness."""
    claims = build_provider_evidence_claims(
        provider_profile=_profile(
            side_buckets={
                "SELL": {"trades": 2, "wins": 1, "losses": 1, "win_rate_percent": 50.0}
            }
        ),
        provider_intelligence=None,
        provider_fingerprint=None,
    )
    ids = {claim["id"] for claim in claims}

    assert "provider.performance.side.SELL" in ids
    assert "provider.performance.side.BUY" not in ids
    assert all(claim["version"] == 633 for claim in claims if claim["source"] == "provider_profile")


def test_zero_sample_bucket_is_not_evidence() -> None:
    claims = build_provider_evidence_claims(
        provider_profile=_profile(
            side_buckets={
                "BUY": {"trades": 0, "wins": 0, "losses": 0, "win_rate_percent": None},
                "SELL": {"trades": 3, "wins": 2, "losses": 1, "win_rate_percent": 66.67},
            }
        ),
        provider_intelligence=None,
        provider_fingerprint=None,
    )
    ids = {claim["id"] for claim in claims}
    assert "provider.performance.side.BUY" not in ids
    assert "provider.performance.side.SELL" in ids


def test_unknown_claim_ref_is_rejected() -> None:
    claims = build_provider_evidence_claims(
        provider_profile=_profile(
            side_buckets={
                "SELL": {"trades": 2, "wins": 1, "losses": 1, "win_rate_percent": 50.0}
            }
        ),
        provider_intelligence=None,
        provider_fingerprint=None,
    )

    with pytest.raises(EvidenceClaimValidationError, match="unsupported_provider_claim_ref"):
        validate_provider_claim_refs(["provider.performance.side.BUY"], claims)


def test_valid_refs_are_deduplicated_without_rewriting_evidence() -> None:
    claims = build_provider_evidence_claims(
        provider_profile=_profile(
            side_buckets={
                "SELL": {"trades": 2, "wins": 1, "losses": 1, "win_rate_percent": 50.0}
            }
        ),
        provider_intelligence=None,
        provider_fingerprint=None,
    )
    refs = validate_provider_claim_refs(
        ["provider.performance.side.SELL", "provider.performance.side.SELL"], claims
    )
    assert refs == ("provider.performance.side.SELL",)


def test_freeform_provider_history_is_rejected_even_with_plausible_language() -> None:
    with pytest.raises(EvidenceClaimValidationError):
        assert_no_freeform_provider_history(
            provider_name="Scalping 📈",
            rationale="The setup is coherent but this provider is historically weaker on BUY.",
            key_factors=["M15 bearish"],
            action_reason="Reduce because their track record is weak on this side.",
        )


def test_current_market_session_language_is_not_mistaken_for_provider_history() -> None:
    assert_no_freeform_provider_history(
        provider_name="Scalping 📈",
        rationale="The BUY fights bearish M15/H1 structure in the London session.",
        key_factors=["Price is already above the entry zone"],
        action_reason="Hold until price returns to the intended entry zone.",
    )
