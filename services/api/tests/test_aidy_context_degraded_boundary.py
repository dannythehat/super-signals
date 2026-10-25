from __future__ import annotations

import pytest

from app.aidy_context_client import _validate_provider_snapshot_boundary


def _complete() -> tuple[dict, dict]:
    return (
        {"capture_status": "complete"},
        {"private_forward_only": True, "live_money_execution_allowed": False},
    )


def _degraded() -> tuple[dict, dict]:
    return (
        {
            "capture_status": "partial",
            "provider_context_evidence_grade": "intraday_complete_d1_missing",
            "provider_context_missing_timeframes": ["1d"],
        },
        {
            "private_forward_only": True,
            "live_money_execution_allowed": False,
            "provider_context_observational_only": True,
            "provider_context_evidence_grade": "intraday_complete_d1_missing",
            "provider_context_missing_timeframes": ["1d"],
            "formal_forward_complete_snapshot_required": True,
        },
    )


def test_complete_snapshot_still_accepted() -> None:
    snapshot, provenance = _complete()
    _validate_provider_snapshot_boundary(snapshot=snapshot, provenance=provenance)


def test_explicit_d1_only_degraded_provider_context_is_accepted() -> None:
    snapshot, provenance = _degraded()
    _validate_provider_snapshot_boundary(snapshot=snapshot, provenance=provenance)


@pytest.mark.parametrize(
    ("mutate_snapshot", "mutate_provenance"),
    [
        ({"provider_context_missing_timeframes": ["4h", "1d"]}, {}),
        ({"provider_context_evidence_grade": "unknown"}, {}),
        ({}, {"provider_context_observational_only": False}),
        ({}, {"formal_forward_complete_snapshot_required": False}),
        ({}, {"live_money_execution_allowed": True}),
        ({}, {"provider_context_missing_timeframes": []}),
        ({}, {"provider_context_evidence_grade": "complete"}),
    ],
)
def test_partial_snapshot_fails_closed_without_exact_aidy_attestation(
    mutate_snapshot: dict, mutate_provenance: dict
) -> None:
    snapshot, provenance = _degraded()
    snapshot.update(mutate_snapshot)
    provenance.update(mutate_provenance)
    with pytest.raises(ValueError, match="aidy_context_partial_snapshot_not_provider_eligible"):
        _validate_provider_snapshot_boundary(snapshot=snapshot, provenance=provenance)


def test_unavailable_snapshot_is_rejected() -> None:
    snapshot, provenance = _degraded()
    snapshot["capture_status"] = "unavailable"
    with pytest.raises(ValueError, match="aidy_context_snapshot_not_complete"):
        _validate_provider_snapshot_boundary(snapshot=snapshot, provenance=provenance)
