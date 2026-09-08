from __future__ import annotations

import inspect

from app.shadow_trading_v3 import ShadowTradeManager, _pit_safe_score_eligibility


def _row(
    *,
    pit_status: str,
    provider_style: str = "unknown",
    score_exclusion_reason: str | None = None,
) -> dict[str, object]:
    return {
        "provider_profile_pit_status": pit_status,
        "provider_style": provider_style,
        "score_exclusion_reason": score_exclusion_reason,
    }


def test_legacy_profile_can_never_regain_score_eligibility() -> None:
    eligible, reason = _pit_safe_score_eligibility(
        _row(
            pit_status="legacy_unresolvable",
            score_exclusion_reason="aidy_m1_revalidation_required",
        ),
        quote_mode="snapshot_poll",
    )
    assert eligible is False
    assert reason == "aidy_m1_revalidation_required"


def test_legacy_profile_uses_canonical_fallback_reason() -> None:
    eligible, reason = _pit_safe_score_eligibility(
        _row(pit_status="legacy_unresolvable"),
        quote_mode="snapshot_poll",
    )
    assert eligible is False
    assert reason == "legacy_profile_unresolvable"


def test_resolved_profile_keeps_existing_market_resolution_policy() -> None:
    eligible, reason = _pit_safe_score_eligibility(
        _row(pit_status="resolved"),
        quote_mode="snapshot_poll",
    )
    assert eligible is True
    assert reason is None


def test_pending_open_transition_uses_pit_safe_gate() -> None:
    source = inspect.getsource(ShadowTradeManager._evaluate_row)
    assert "_pit_safe_score_eligibility(" in source
    assert "score_eligible=:eligible" in source
