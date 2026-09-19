from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "versions" / "0104_aidy_grounding_health.py"


def test_grounding_health_migration_is_chained_and_read_only() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "0104_aidy_grounding_health"' in source
    assert 'down_revision: str | None = "0103_aidy_gold_state"' in source
    assert len("0104_aidy_grounding_health") <= 32
    assert "CREATE VIEW aidy_reasoning_grounding_health AS" in source
    assert "suspicious_legacy_provider_history_rows" in source
    assert "unsupported_forward_rows" in source
    assert "UPDATE aidy_reasoning_annotations" not in source
    assert "DELETE FROM aidy_reasoning_annotations" not in source
