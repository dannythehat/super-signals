from __future__ import annotations

from pathlib import Path

from app import aidy_shadow_runtime, provider_day21_full_chain
from app.provider_day17_confidence_sizing import (
    LIVE_VARIABLE_SIZING_ALLOWED,
    PAPER_VARIABLE_SIZING_ALLOWED,
)
from app.provider_day18_combined_book import PRODUCTION_PORTFOLIO_AUTHORITY
from app.provider_day20_management_counterfactual import (
    LIVE_MANAGEMENT_ALLOWED,
    PAPER_MANAGEMENT_ALLOWED,
)


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "versions" / "0070_provider_day21_full_chain.py"
RUNTIME = ROOT / "app" / "provider_day21_full_chain.py"
AIDY_RUNTIME = ROOT / "app" / "aidy_shadow_runtime.py"


def test_day21_contract_is_research_only_and_all_execution_authority_stays_off() -> None:
    assert provider_day21_full_chain.CONTRACT_VERSION == "provider_day21_chain_v1"
    assert provider_day21_full_chain.FORWARD_ANCHOR_MAX_DELAY_SECONDS == 300
    assert LIVE_VARIABLE_SIZING_ALLOWED is False
    assert PAPER_VARIABLE_SIZING_ALLOWED is False
    assert PRODUCTION_PORTFOLIO_AUTHORITY is False
    assert LIVE_MANAGEMENT_ALLOWED is False
    assert PAPER_MANAGEMENT_ALLOWED is False


def test_day21_migration_is_after_day20_append_only_and_hard_walls_authority() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "0070_provider_day21_full_chain"' in source
    assert 'down_revision: str | None = "0069_provider_day20_management"' in source
    assert "Provider Day 21 chain evidence is append-only" in source
    assert "BEFORE UPDATE OR DELETE ON provider_day21_signal_chains" in source
    assert "BEFORE UPDATE OR DELETE ON provider_day21_chain_events" in source
    for fragment in (
        "CHECK (research_only)",
        "CHECK (NOT live_money_execution_allowed)",
        "CHECK (NOT paper_execution_allowed)",
        "CHECK (NOT live_variable_sizing_allowed)",
        "CHECK (NOT paper_variable_sizing_allowed)",
        "CHECK (NOT live_management_allowed)",
        "CHECK (NOT paper_management_allowed)",
        "CHECK (NOT public_broadcast_allowed)",
    ):
        assert fragment in source


def test_every_accepted_shadow_signal_gets_one_immutable_anchor() -> None:
    source = RUNTIME.read_text(encoding="utf-8")
    assert "src.status='shadow' AND s.parser_status='accepted'" in source
    assert "ON CONFLICT (signal_id) DO NOTHING" in source
    assert "signal_id uuid NOT NULL UNIQUE" in MIGRATION.read_text(encoding="utf-8")
    assert "anchor_delay_seconds" in source
    assert "interval '5 minutes'" in source


def test_day21_does_not_hindsight_match_day13_realized_duration_cells() -> None:
    source = RUNTIME.read_text(encoding="utf-8")
    assert "evidence=[]" in source
    assert "realized_descriptive_post_entry" in source
    assert "cannot be known at signal time" in source
    assert "historical_unscored_no_forward_decision" in source


def test_missing_context_is_not_fabricated_and_terminal_miss_is_explicit() -> None:
    source = RUNTIME.read_text(encoding="utf-8")
    assert "provider_signal_context_attachments" in source
    assert "provider_signal_context_terminal_misses" in source
    assert 'return "terminal_miss", dict(missed)' in source
    # An enrolled signal with neither attachment nor terminal miss exits and retries;
    # it never receives a made-up context stage.
    assert "elif context_result is None:\n                return inserted" in source


def test_day19_shadow_explanation_is_forced_internal_only() -> None:
    source = RUNTIME.read_text(encoding="utf-8")
    assert "placed=False" in source
    assert 'status="shadow_internal_only"' in source
    assert "public_broadcast_allowed" in MIGRATION.read_text(encoding="utf-8")


def test_day20_does_not_invent_management_decisions() -> None:
    source = RUNTIME.read_text(encoding="utf-8")
    assert "waiting_for_genuine_forward_management_evidence" in source
    assert '"management_decision_invented": False' in source


def test_terminal_shadow_outcome_is_append_only_feedback() -> None:
    source = RUNTIME.read_text(encoding="utf-8")
    assert "status IN ('closed','cancelled','missed')" in source
    assert 'stage="outcome_feedback"' in source
    assert 'event_key="terminal_shadow_outcome_v1"' in source
    assert "quality_r_multiple" in source


def test_day21_runs_after_context_inside_existing_application_owned_loop() -> None:
    source = AIDY_RUNTIME.read_text(encoding="utf-8")
    context_index = source.index("context_resolver.resolve_once()")
    day21_index = source.index("day21_resolver.resolve_once()")
    assert context_index < day21_index
    assert "ProviderDay21ChainResolver(self._session_factory)" in source
    assert "asyncio.create_task" in source
    assert "Provider Day21 full-chain loop failed safely" in source
    # Day 21 resolver itself is DB-only: no MetaAPI/MT5/Vantage gateway imports.
    day21_source = RUNTIME.read_text(encoding="utf-8").lower()
    assert "metaapi" not in day21_source
    assert "vantage" not in day21_source
    assert "mt5" not in day21_source
