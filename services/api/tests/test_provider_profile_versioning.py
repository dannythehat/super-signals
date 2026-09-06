from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "versions" / "0056_provider_profile_versions.py"


def _migration_source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_day7_profile_history_chains_from_aidy_market_truth() -> None:
    source = _migration_source()
    assert 'revision: str = "0056_provider_profile_versions"' in source
    assert 'down_revision: str | None = "0055_aidy_provider_lab_truth"' in source
    # Production previously failed on an Alembic revision longer than VARCHAR(32).
    assert len("0056_provider_profile_versions") <= 32


def test_day7_history_is_append_only_and_versions_identity_and_profile() -> None:
    source = _migration_source()
    assert "provider_research_profile_versions" in source
    assert "BEFORE UPDATE OR DELETE ON provider_research_profile_versions" in source
    assert "provider_research_profile_versions are append-only" in source
    assert "'chat_id', s.chat_id" in source
    assert "'chat_title', s.chat_title" in source
    assert "'source_alias', s.source_alias" in source
    assert "'status', s.status" in source
    assert "'research_state', p.research_state" in source
    assert "'style', p.style" in source
    assert "'profile_metadata'" in source


def test_day7_periodic_refresh_does_not_create_fake_versions() -> None:
    source = _migration_source()
    # adaptive-provider-v1 changes generated_at_epoch on every build. It is a
    # refresh clock, not new provider intelligence, so it must not drive history.
    assert "#- '{adaptive_v1,generated_at_epoch}'" in source
    assert "v_latest_fingerprint = v_fingerprint" in source
    assert "RETURN NULL;" in source


def test_day7_bootstrap_is_forward_only_and_not_backdated() -> None:
    source = _migration_source()
    assert "'bootstrap_current_state'" in source
    assert "server_default=sa.func.now()" in source
    assert "does not fabricate earlier" in source
    assert "historical Telegram or performance data" in source


def test_day7_as_of_reader_cannot_leak_future_profile_state() -> None:
    source = _migration_source()
    assert "provider_research_profile_version_as_of" in source
    assert "v.effective_at <= p_as_of" in source
    assert "ORDER BY v.effective_at DESC, v.version_no DESC" in source
    assert "LANGUAGE sql STABLE" in source


def test_day7_serializes_per_provider_version_allocation() -> None:
    source = _migration_source()
    assert "pg_advisory_xact_lock" in source
    assert "provider-profile-version:" in source
    assert "COALESCE(v_latest_version, 0) + 1" in source
    assert "uq_provider_profile_source_version" in source


def test_day7_identity_changes_are_captured_without_waiting_for_profile_rebuild() -> None:
    source = _migration_source()
    assert "AFTER UPDATE OF chat_id, chat_title, source_alias, status ON sources" in source
    assert "OLD.chat_id IS DISTINCT FROM NEW.chat_id" in source
    assert "OLD.status IS DISTINCT FROM NEW.status" in source
    assert "'source_identity_update'" in source
