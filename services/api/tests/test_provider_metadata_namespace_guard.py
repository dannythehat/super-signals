from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "versions" / "0065_provider_metadata_guard.py"
PLAYBOOK_MIGRATION = ROOT / "migrations" / "versions" / "0115_provider_playbook_guard.py"
PROVIDER_RESEARCH = ROOT / "app" / "provider_research.py"


def test_provider_metadata_guard_chains_from_current_head() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "0065_provider_metadata_guard"' in source
    assert 'down_revision: str | None = "0064_provider_context_terminal"' in source
    assert len("0065_provider_metadata_guard") <= 32


def test_discovery_updates_cannot_erase_adaptive_or_footprint_namespaces() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert "BEFORE UPDATE OF profile_metadata ON provider_research_profiles" in source
    assert "OLD.profile_metadata->'adaptive_v1'" in source
    assert "OLD.profile_metadata->'footprint_v1'" in source
    assert "NOT NEW.profile_metadata ? 'adaptive_v1'" in source
    assert "NOT NEW.profile_metadata ? 'footprint_v1'" in source


def test_owning_writer_can_explicitly_replace_reserved_namespace() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    # Preservation only fires when NEW omits the key. If adaptive/footprint writers
    # supply the namespace, their new payload passes through unchanged.
    assert "AND NOT NEW.profile_metadata ? 'adaptive_v1' THEN" in source
    assert "AND NOT NEW.profile_metadata ? 'footprint_v1' THEN" in source



def test_playbook_guard_preserves_management_and_playbook_namespaces() -> None:
    source = PLAYBOOK_MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "0115_provider_playbook_guard"' in source
    assert 'down_revision: str | None = "0114_enable_shadow_providers"' in source
    assert len("0115_provider_playbook_guard") <= 32
    assert "OLD.profile_metadata->'management_language_audit_v1'" in source
    assert "NOT NEW.profile_metadata ? 'management_language_audit_v1'" in source
    assert "OLD.profile_metadata->'provider_playbook_v1'" in source
    assert "NOT NEW.profile_metadata ? 'provider_playbook_v1'" in source


def test_provider_discovery_merges_metadata_instead_of_erasing_learned_namespaces() -> None:
    source = PROVIDER_RESEARCH.read_text(encoding="utf-8")
    assert (
        "profile_metadata=COALESCE(provider_research_profiles.profile_metadata,'{}'::jsonb)"
        in source
    )
    assert "|| EXCLUDED.profile_metadata" in source
    assert "profile_metadata=EXCLUDED.profile_metadata" not in source
