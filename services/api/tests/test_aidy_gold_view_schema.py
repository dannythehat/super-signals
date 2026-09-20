from pathlib import Path


MIGRATION = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "0109_aidy_gold_view.py"
)


def test_gold_view_migration_is_backward_compatible_and_bounded() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "0109_aidy_gold_view"' in source
    assert 'down_revision: str | None = "0108_aidy_hist_stress_schema"' in source
    assert "ADD COLUMN gold_view_direction varchar(16)" in source
    assert "ADD COLUMN gold_view_confidence numeric" in source
    assert "ADD COLUMN gold_view_horizon_minutes integer" in source
    assert "ADD COLUMN gold_view_reason text" in source
    assert "ADD COLUMN provider_alignment varchar(16)" in source
    assert "NOT NULL" not in source.split("def upgrade()", 1)[1].split("def downgrade()", 1)[0]


def test_gold_view_schema_constrains_only_research_annotation_fields() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert "bullish" in source
    assert "bearish" in source
    assert "neutral" in source
    assert "unknown" in source
    assert "aligned" in source
    assert "conflicts" in source
    assert "unclear" in source
    assert "gold_view_confidence >= 0" in source
    assert "gold_view_confidence <= 1" in source
    assert "gold_view_horizon_minutes <= 240" in source
    lower = source.lower()
    assert "live_money" not in lower
    assert "broker" not in lower
