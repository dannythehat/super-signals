from pathlib import Path

from app.provider_context_attachment_v2 import TERMINAL_MISS_CONTRACT_VERSION_V2

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parents[1]


def test_context_v2_replays_v1_misses_without_mutating_audit_history() -> None:
    source = (ROOT / "app" / "provider_context_attachment_v2.py").read_text(encoding="utf-8")
    assert TERMINAL_MISS_CONTRACT_VERSION_V2 == "provider_aidy_context_terminal_miss_v2"
    assert "x.contract_version=:contract_version" in source
    assert "ON CONFLICT (signal_id,contract_version) DO NOTHING" in source
    assert "UPDATE provider_signal_context_terminal_misses" not in source
    assert "DELETE FROM provider_signal_context_terminal_misses" not in source
    assert "MetaApi" not in source
    assert "broker" in source.casefold()  # documentation explicitly states broker isolation


def test_context_v2_recovery_is_bounded_and_research_only() -> None:
    source = (ROOT / "app" / "provider_context_v2_recovery.py").read_text(encoding="utf-8")
    assert "_MAX_PASSES = 200" in source
    assert "ProviderContextAttachmentResolverV2" in source
    assert "AidyContextClient.from_environment" in source
    assert "place_market_order" not in source
    assert "MetaApiTradeGateway" not in source


def test_day13_refresh_uses_forward_shadow_evidence_and_daily_key() -> None:
    source = (ROOT / "app" / "provider_day13_runtime.py").read_text(encoding="utf-8")
    assert "FROM shadow_trades t" in source
    assert "t.score_eligible" in source
    assert "provider_signal_context_attachments" in source
    assert "a.aidy_context_as_of_utc<=a.signal_posted_at" in source
    assert "run_day=:run_day" in source
    assert "provider_context_v2_recovery" in source
    assert "provider_trade_scores" not in source
    assert "aidy_m1_retrospective" not in source
    assert "live_money_execution_allowed\": False" in source


def test_learning_runtime_migration_preserves_sha_and_versions_terminal_misses() -> None:
    migration = (
        ROOT / "migrations" / "versions" / "0088_aidy_learning_runtime.py"
    ).read_text(encoding="utf-8")
    assert 'down_revision: str | None = "0087_provider_scoreboard"' in migration
    assert "uq_provider_conditional_run_model_sha_day" in migration
    assert '["model_version", "code_sha", "run_day"]' in migration
    assert "uq_provider_context_terminal_miss_signal_contract" in migration
    assert '["signal_id", "contract_version"]' in migration


def test_startup_runs_day13_and_day14_as_independent_long_lived_research_loops() -> None:
    startup = (REPO_ROOT / "scripts" / "render-start.sh").read_text(encoding="utf-8")
    assert "python -m app.provider_day13_runtime --forever &" in startup
    assert "python -m app.provider_day14_governance &" in startup
    assert "python -m app.provider_day13_runtime &&" not in startup
