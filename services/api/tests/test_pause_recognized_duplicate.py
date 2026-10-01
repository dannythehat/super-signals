from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "versions" / "0057_pause_recognized_gtmo_duplicate.py"


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_duplicate_quarantine_chains_after_day7_profile_history() -> None:
    source = _source()
    assert 'revision: str = "0057_pause_gtmo_duplicate"' in source
    assert 'down_revision: str | None = "0056_provider_profile_versions"' in source
    assert len("0057_pause_gtmo_duplicate") <= 32


def test_only_confirmed_shadow_duplicate_is_paused() -> None:
    source = _source()
    assert 'Gold Trader Mo🤴🏽' in source
    assert 'GTMO VIP 🤴🏽' in source
    assert "SET status = 'paused'" in source
    assert "AND status <> 'revoked'" in source
    assert "status IN ('testing', 'live')" in source


def test_duplicate_evidence_is_recorded_not_deleted() -> None:
    source = _source()
    assert "duplicate_of_source_id" in source
    assert "duplicate_score = 0.75600" in source
    assert "'overlap_messages', 341" in source
    assert "'overlap_ratio', 0.756" in source
    assert "'threshold', 0.60" in source
    assert "DELETE FROM sources" not in source
    assert "DELETE FROM provider_research_profiles" not in source
