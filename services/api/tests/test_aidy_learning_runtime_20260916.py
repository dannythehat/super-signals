from pathlib import Path

from app.provider_context_attachment import TERMINAL_MISS_CONTRACT_VERSION
from app.provider_context_attachment_v2 import TERMINAL_MISS_CONTRACT_VERSION_V2

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parents[1]


def test_context_v2_replays_only_v1_misses_without_mutating_audit_history() -> None:
    source = (ROOT / "app" / "provider_context_attachment_v2.py").read_text(encoding="utf-8")
    assert TERMINAL_MISS_CONTRACT_VERSION_V2 == "provider_aidy_context_terminal_miss_v2"
    assert TERMINAL_MISS_CONTRACT_VERSION == "provider_aidy_context_terminal_miss_v1"
    assert "JOIN provider_signal_context_terminal_misses v1" in source
    assert "v1.contract_version=:v1_contract_version" in source
    assert "x.contract_version=:v2_contract_version" in source
    assert "ON CONFLICT (signal_id,contract_version) DO NOTHING" in source
    assert "UPDATE provider_signal_context_terminal_misses" not in source
    assert "DELETE FROM provider_signal_context_terminal_misses" not in source
    assert "MetaApi" not in source
    assert "broker" in source.casefold()


def test_context_v2_recovery_is_bounded_and_research_only() -> None:
    source = (ROOT / "app" / "provider_context_v2_recovery.py").read_text(encoding="utf-8")
    assert "_MAX_PASSES = 200" in source
    assert "ProviderContextAttachmentResolverV2" in source
    assert "AidyContextClient.from_environment" in source
    assert "place_market_order" not in source
    assert "MetaApiTradeGateway" not in source


def test_provider_learning_refresh_is_evidence_driven_not_deploy_or_day_driven() -> None:
    source = (ROOT / "app" / "provider_day13_runtime.py").read_text(encoding="utf-8")
    assert "FROM shadow_trades t" in source
    assert "t.score_eligible" in source
    assert "provider_signal_context_attachments" in source
    assert "a.aidy_context_as_of_utc<=a.signal_posted_at" in source
    assert "evidence_digest=:digest" in source
    assert "skipped_unchanged_evidence" in source
    assert "SUPER_SIGNALS_PROVIDER_LEARNING_SECONDS" in source
    assert "_DEFAULT_REFRESH_SECONDS = 900" in source
    assert "provider_context_v2_recovery" in source
    assert "provider_trade_scores" not in source
    assert "aidy_m1_retrospective" not in source
    assert "run_day" not in source
    assert '"live_money_execution_allowed": False' in source


def test_learning_runtime_migration_keys_runs_by_forward_evidence_digest() -> None:
    migration = (
        ROOT / "migrations" / "versions" / "0088_aidy_learning_runtime.py"
    ).read_text(encoding="utf-8")
    assert 'down_revision: str | None = "0087_provider_scoreboard"' in migration
    assert "uq_provider_conditional_run_model_sha_evidence" in migration
    assert '["model_version", "code_sha", "evidence_digest"]' in migration
    assert "evidence_digest IS NOT NULL" in migration
    assert "run_day" not in migration
    assert "uq_provider_context_terminal_miss_signal_contract" in migration
    assert '["signal_id", "contract_version"]' in migration


def test_governance_v2_uses_one_immutable_boundary_per_provider() -> None:
    source = (ROOT / "app" / "provider_governance_runtime_v2.py").read_text(encoding="utf-8")
    assert "GROUP BY source_id" in source
    assert "COUNT(DISTINCT preregistered_at) AS boundary_count" in source
    assert "provider_preregistration_boundary_not_frozen" in source
    assert "boundaries[source_id]" in source
    assert '"provider_frozen_oos_boundary"' in source
    assert "authoritative_live_transition_count" in source
    assert '"live_money_execution_allowed": False' in source
    assert "MetaApi" not in source
    assert "place_market_order" not in source


def test_startup_runs_learning_and_governance_as_independent_research_loops() -> None:
    startup = (REPO_ROOT / "scripts" / "render-start.sh").read_text(encoding="utf-8")
    assert "python -m app.provider_day13_runtime --forever &" in startup
    assert "python -m app.provider_governance_runtime_v2 &" in startup
    assert "python -m app.provider_day13_runtime &&" not in startup
