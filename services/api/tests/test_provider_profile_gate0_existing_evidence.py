from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "versions" / "0071_provider_profile_gate0_existing_evidence.py"


def test_existing_scoreable_evidence_is_revalidated_using_its_signal_time_profile() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert "UPDATE shadow_trades t" in source
    assert "WHERE t.score_eligible" in source
    assert "v.id=t.provider_profile_version_id" in source
    assert "v.source_id=t.source_id" in source
    assert "v.effective_at<=t.signal_posted_at" in source
    assert "provider_profile_gate0_snapshot_qualified(v.profile_snapshot)" in source
    assert "score_exclusion_reason='provider_profile_gate_incomplete'" in source
    assert "pnl_percent=NULL" in source


def test_downgrade_never_manufactures_historical_score_eligibility() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    downgrade = source.split("def downgrade()", 1)[1]
    assert "score_eligible=true" not in downgrade.lower()
