from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_day12_source_admits_only_forward_pit_score_eligible_paper_evidence() -> None:
    source = (ROOT / "app" / "provider_day12_fingerprint.py").read_text(encoding="utf-8")
    assert "t.score_eligible" in source
    assert "t.provider_profile_pit_status='RESOLVED'" in source
    assert "s.status IN ('shadow','testing')" in source
    assert "broker_deals" not in source
    assert "MetaApi" not in source
    assert "metaapi" not in source.casefold()
    assert "provider_execution_reconciliation" not in source


def test_day12_raw_rates_are_descriptive_not_primary() -> None:
    source = (ROOT / "app" / "provider_day12_fingerprint.py").read_text(encoding="utf-8")
    assert 'PRIMARY_ESTIMATE_KIND = "posterior_partial_pool"' in source
    assert "raw_hit_rate_descriptive_only" in source
    assert "raw_values_are_descriptive_only" in source
    assert 'STATISTICAL_STATUS = "WAITING-FOR-FORWARD-EVIDENCE"' in source


def test_day12_migration_hard_walls_authority() -> None:
    migration = (ROOT / "migrations" / "versions" / "0061_provider_day12_fingerprint.py").read_text(encoding="utf-8")
    assert "ck_provider_fingerprint_stat_authority_waiting" in migration
    assert "ck_provider_fingerprint_cell_waiting" in migration
    assert "ck_provider_fingerprint_run_research_only" in migration
    assert "ck_provider_fingerprint_run_no_live_money" in migration
    assert "ck_provider_fingerprint_cell_no_live_money" in migration
    assert "WAITING-FOR-FORWARD-EVIDENCE" in migration
