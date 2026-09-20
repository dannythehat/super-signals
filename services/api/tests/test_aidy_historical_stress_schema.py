from pathlib import Path


def test_historical_stress_migration_expands_only_research_schema_contracts() -> None:
    migration = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "0108_aidy_hist_stress_schema.py"
    ).read_text()

    assert 'down_revision: str | None = "0107_aidy_hist_replay_v2"' in migration
    assert "'research_train'" in migration
    assert "'research_validation'" in migration
    assert "'research_oos'" in migration
    assert "'reconstructed_research'" in migration
    assert "'train_validation'" in migration
    assert "ALTER COLUMN partition TYPE varchar(32)" in migration
    assert "ck_aidy_hist_case_partition" in migration
    assert "ck_aidy_hist_score_partition" in migration
    assert "ck_aidy_hist_case_tier" in migration
    assert "ck_aidy_hist_run_scope" in migration
    assert 'DROP VIEW IF EXISTS aidy_historical_replay_scoreboard' in migration
    assert 'CREATE VIEW aidy_historical_replay_scoreboard AS' in migration


def test_historical_stress_migration_does_not_add_execution_authority() -> None:
    migration = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "0108_aidy_hist_stress_schema.py"
    ).read_text().lower()

    assert "live_money_execution_allowed" not in migration
    assert "broker" not in migration
    assert "risk_multiplier" not in migration
