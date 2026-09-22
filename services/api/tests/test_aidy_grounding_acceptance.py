from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from app.aidy_evidence_contract import EVIDENCE_CONTRACT_VERSION
from app.aidy_grounding_acceptance import GroundingAuditError, audit_grounded_row

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "versions" / "0105_aidy_grounding_accept.py"
MAIN = ROOT / "app" / "main.py"


def _row() -> dict:
    signal_at = datetime(2026, 9, 21, 22, 0, tzinfo=UTC)
    return {
        "id": uuid4(),
        "created_at": signal_at + timedelta(seconds=20),
        "evidence_contract_version": EVIDENCE_CONTRACT_VERSION,
        "claim_validation_status": "passed",
        "unsupported_claim_count": 0,
        "provider_evidence_snapshot": [
            {
                "id": "provider.performance.side.BUY",
                "kind": "provider_side_performance",
                "source": "provider_profile",
                "path": "provider_profile.performance.side_buckets.BUY",
                "value": {
                    "trades": 20,
                    "wins": 15,
                    "losses": 5,
                    "win_rate_percent": 75.0,
                },
                "sample_n": 20,
                "version": 633,
                "as_of_utc": (signal_at - timedelta(minutes=2)).isoformat(),
            }
        ],
        "provider_claim_refs": ["provider.performance.side.BUY"],
        "provider_profile_version_no": 633,
        "rationale": "The entry remains within the intended zone and M15 is aligned.",
        "key_factors": ["Entry still valid", "M15 aligned"],
        "shadow_action_reason": "Take at the configured shadow risk.",
        "signal_posted_at": signal_at,
        "provider_name": "Example Provider",
        "source_id": uuid4(),
    }


def test_valid_forward_row_passes_full_grounding_audit() -> None:
    audit_grounded_row(_row())


def test_claim_ref_must_exist_in_frozen_snapshot() -> None:
    row = _row()
    row["provider_claim_refs"] = ["provider.performance.session.asia"]
    with pytest.raises(GroundingAuditError, match="provider_claim_ref_missing_from_snapshot"):
        audit_grounded_row(row)


def test_future_provider_evidence_is_rejected() -> None:
    row = _row()
    row["provider_evidence_snapshot"][0]["as_of_utc"] = (
        row["signal_posted_at"] + timedelta(seconds=1)
    ).isoformat()
    with pytest.raises(GroundingAuditError, match="future_provider_evidence"):
        audit_grounded_row(row)


def test_provider_profile_version_must_match_frozen_annotation_version() -> None:
    row = _row()
    row["provider_evidence_snapshot"][0]["version"] = 632
    with pytest.raises(GroundingAuditError, match="provider_profile_version_mismatch"):
        audit_grounded_row(row)


def test_freeform_provider_history_is_rejected_again_at_acceptance() -> None:
    row = _row()
    row["rationale"] = "This provider is historically stronger on BUY."
    with pytest.raises(GroundingAuditError):
        audit_grounded_row(row)


def test_nonzero_unsupported_count_is_rejected() -> None:
    row = _row()
    row["unsupported_claim_count"] = 1
    with pytest.raises(GroundingAuditError, match="unsupported_claim_count_nonzero"):
        audit_grounded_row(row)


def test_acceptance_migration_is_research_only_and_has_no_live_money_path() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "0105_aidy_grounding_accept"' in source
    assert 'down_revision: str | None = "0104_aidy_grounding_health"' in source
    assert len("0105_aidy_grounding_accept") <= 32
    assert "research_only boolean NOT NULL DEFAULT true" in source
    assert "live_money_execution_allowed boolean NOT NULL DEFAULT false" in source
    assert "CHECK (live_money_execution_allowed = false)" in source


def test_main_lifecycle_registers_acceptance_monitor_in_isolated_research_lane() -> None:
    source = MAIN.read_text(encoding="utf-8")
    assert '("aidy_grounding_acceptance_runtime", AidyGroundingAcceptanceRuntime)' in source
    assert "runtime = runtime_type(research_session_factory)" in source
    assert 'name="super-signals-research-startup"' in source
    assert source.index("await publisher.start()") < source.index(
        'name="super-signals-research-startup"'
    )
    assert "await asyncio.wait_for(runtime.stop(), timeout=5)" in source
