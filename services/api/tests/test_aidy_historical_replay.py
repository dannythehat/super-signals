from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.aidy_historical_replay import (
    INPUT_CONTRACT_VERSION,
    REPLAY_VERSION,
    _assert_no_future_fields,
    _partition,
    _reason_with_provider_claim_retry,
    _scope_from_env,
    _shadow_score,
    _signal_context_from_payload,
)
from app.aidy_reasoning_engine import AidyReasoningUnavailable

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "versions" / "0106_aidy_hist_replay.py"
VERSIONING_MIGRATION = ROOT / "migrations" / "versions" / "0107_aidy_hist_replay_v2.py"
MODULE = ROOT / "app" / "aidy_historical_replay.py"
MAIN = ROOT / "app" / "main.py"


def _payload() -> dict:
    return {
        "input_contract_version": INPUT_CONTRACT_VERSION,
        "source_decision_id": "11111111-1111-1111-1111-111111111111",
        "source_id": "22222222-2222-2222-2222-222222222222",
        "signal_posted_at": "2026-09-17T12:00:00+00:00",
        "provider_name_for_validation_only": "Example Provider",
        "signal": {
            "side": "BUY",
            "symbol": "XAUUSD",
            "entry_low": "4380",
            "entry_high": "4382",
            "stop_loss": "4370",
            "take_profits": ["4390", "4400"],
        },
        "deterministic_decision": {
            "decision_class": "approve",
            "reasons": [{"code": "research"}],
        },
        "provider_profile_version_no": 42,
        "provider_profile_effective_at": "2026-09-17T11:55:00+00:00",
        "provider_evidence_claims": [
            {
                "id": "provider.performance.side.BUY",
                "kind": "provider_side_performance",
                "source": "provider_profile",
                "path": "provider_profile.performance.side_buckets.BUY",
                "value": {"trades": 20, "wins": 12, "losses": 8},
                "sample_n": 20,
                "version": 42,
                "as_of_utc": "2026-09-17T11:55:00+00:00",
            }
        ],
        "market_context": {
            "source": "immutable_signal_attachment",
            "as_of_utc": "2026-09-17T11:59:00+00:00",
            "session": "london",
            "trend_structure": "mixed",
        },
        "recent_messages": [
            {"posted_at": "2026-09-17T11:58:00+00:00", "text": "XAUUSD BUY"}
        ],
        "self_calibration": None,
        "supplemental_evidence": None,
        "pit_assertions": {
            "profile_effective_at_lte_signal": True,
            "context_as_of_lte_signal": True,
            "recent_messages_lte_signal": True,
            "outcome_values_absent": True,
        },
    }


def test_partition_boundaries_are_frozen_chronologically() -> None:
    assert _partition(datetime(2026, 9, 17, 13, 20, 25, tzinfo=UTC)) == "development"
    assert _partition(datetime(2026, 9, 17, 13, 20, 26, tzinfo=UTC)) == "validation"
    assert _partition(datetime(2026, 9, 18, 8, 32, 44, tzinfo=UTC)) == "validation"
    assert _partition(datetime(2026, 9, 18, 8, 32, 45, tzinfo=UTC)) == "holdout"


def test_future_outcome_fields_are_forbidden_anywhere_in_input() -> None:
    clean = _payload()
    _assert_no_future_fields(clean)

    dirty = _payload()
    dirty["market_context"]["actual_pnl_usd"] = 123
    with pytest.raises(ValueError, match="historical_replay_future_field"):
        _assert_no_future_fields(dirty)


def test_signal_context_is_built_only_from_frozen_pretrade_payload() -> None:
    context = _signal_context_from_payload(_payload())
    assert context.decision_id == "11111111-1111-1111-1111-111111111111"
    assert context.provider_name == ""
    assert context.side == "BUY"
    assert context.symbol == "XAUUSD"
    assert context.provider_evidence_claims[0]["sample_n"] == 20
    assert context.market_context["source"] == "immutable_signal_attachment"
    assert context.decision_reasons == [{"code": "research"}]
    assert context.self_calibration is None
    assert context.supplemental_evidence is None


def test_signal_context_rejects_failed_pit_assertion() -> None:
    payload = _payload()
    payload["pit_assertions"]["context_as_of_lte_signal"] = False
    with pytest.raises(ValueError, match="historical_replay_pit_assertion_not_clean"):
        _signal_context_from_payload(payload)


@pytest.mark.parametrize(
    ("action", "multiplier", "actual", "expected_shadow", "expected_delta"),
    [
        ("take", "1", "100", "100", "0"),
        ("reduce", "0.5", "100", "50.0", "-50.0"),
        ("reduce", "0.25", "-80", "-20.00", "60.00"),
        ("hold", "0", "100", "0", "-100"),
        ("reject", "0", "-75", "0", "75"),
        ("need_more_evidence", "0", "25", "0", "-25"),
    ],
)
def test_shadow_scoring_is_deterministic(
    action: str,
    multiplier: str,
    actual: str,
    expected_shadow: str,
    expected_delta: str,
) -> None:
    shadow, delta = _shadow_score(
        action=action,
        risk_multiplier=Decimal(multiplier),
        actual_pnl_usd=Decimal(actual),
    )
    assert shadow == Decimal(expected_shadow)
    assert delta == Decimal(expected_delta)


def test_invalid_reduce_multiplier_fails_closed() -> None:
    with pytest.raises(ValueError, match="historical_replay_reduce_multiplier_invalid"):
        _shadow_score(
            action="reduce",
            risk_multiplier=Decimal("1"),
            actual_pnl_usd=Decimal("100"),
        )


def test_holdout_scope_is_locked_without_explicit_open_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIDY_HISTORICAL_REPLAY_SCOPE", "holdout")
    monkeypatch.delenv("AIDY_HISTORICAL_REPLAY_OPEN_HOLDOUT", raising=False)
    assert _scope_from_env() == "development_validation"

    monkeypatch.setenv("AIDY_HISTORICAL_REPLAY_OPEN_HOLDOUT", "1")
    assert _scope_from_env() == "holdout"


def test_materialization_query_does_not_select_future_outcome_values() -> None:
    source = MODULE.read_text(encoding="utf-8")
    select_block = source.split('_MATERIALIZE_SELECT = text(', 1)[1].split('_INSERT_CASE = text(', 1)[0]
    assert "EXISTS (" in select_block
    assert "aidy_decision_outcomes" in select_block
    for forbidden in (
        "actual_pnl_usd",
        "actual_realized_r",
        "baseline_pnl_usd",
        "decision_delta_usd",
        "resolved_at",
    ):
        assert forbidden not in select_block


def test_outcomes_are_joined_only_after_replay_decision_exists() -> None:
    source = MODULE.read_text(encoding="utf-8")
    score_block = source.split('_SELECT_UNSCORED = text(', 1)[1].split('_INSERT_SCORE = text(', 1)[0]
    assert "FROM aidy_historical_replay_decisions rd" in score_block
    assert "JOIN aidy_decision_outcomes ao" in score_block


def test_replay_migration_is_research_only_and_execution_is_impossible() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "0106_aidy_hist_replay"' in source
    assert 'down_revision: str | None = "0105_aidy_grounding_accept"' in source
    assert len("0106_aidy_hist_replay") <= 32
    assert source.count("CHECK (research_only=true)") >= 4
    assert source.count("CHECK (live_money_execution_allowed=false)") >= 4
    assert "aidy_historical_replay_scoreboard" in source


def test_runtime_is_disabled_by_default_and_has_separate_enable_flag() -> None:
    source = MODULE.read_text(encoding="utf-8")
    assert 'AIDY_HISTORICAL_REPLAY_ENABLED", "0"' in source
    assert "AIDY_HISTORICAL_REPLAY_OPEN_HOLDOUT" in source
    assert "AIDY_HISTORICAL_REPLAY_MAX_CALLS" in source
    assert REPLAY_VERSION in source


def test_frozen_cohort_short_circuits_rematerialization_after_140_cases() -> None:
    source = MODULE.read_text(encoding="utf-8")
    assert "_EXPECTED_FROZEN_CASES = 140" in source
    assert "if existing >= _EXPECTED_FROZEN_CASES" in source
    assert "return 0" in source


def test_main_lifecycle_starts_and_stops_replay_runtime_safely() -> None:
    source = MAIN.read_text(encoding="utf-8")
    construct = "aidy_historical_replay_runtime = AidyHistoricalReplayRuntime(research_session_factory)"
    start = "await aidy_historical_replay_runtime.start()"
    stop = "await aidy_historical_replay_runtime.stop()"
    assert construct in source
    assert start in source
    assert stop in source
    assert source.index(construct) < source.index(start) < source.index("try:\n        yield")
    assert source.index(stop) > source.index("finally:")


def test_materializer_inherits_legacy_reason_exclusion_from_frozen_previous_contract() -> None:
    source = MODULE.read_text(encoding="utf-8")
    assert '_PREVIOUS_INPUT_CONTRACT_VERSION = "aidy_historical_replay_input_v8"' in source
    assert "payload = dict(base)" in source
    assert 'provenance["derived_from_input_contract_version"]' in source
    assert 'provenance["same_frozen_source_decision"] = True' in source
    assert 'pit["derived_from_previous_frozen_contract"] = True' in source
    # Build 5 must not reconstruct or reintroduce legacy decision reasons.
    materialize = source.split("    def materialize(", 1)[1].split("    def _selected_cases", 1)[0]
    assert 'payload["deterministic_decision"]' not in materialize
    assert "d.reasons" not in materialize


def test_replay_versions_cases_and_decisions_without_rewriting_prior_exams() -> None:
    source = VERSIONING_MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "0107_aidy_hist_replay_v2"' in source
    assert 'down_revision: str | None = "0106_aidy_hist_replay"' in source
    assert "UNIQUE (source_decision_id,input_contract_version)" in source
    assert "UNIQUE (case_id,replay_version)" in source

    module = MODULE.read_text(encoding="utf-8")
    assert 'REPLAY_VERSION = "aidy_historical_time_machine_v10_effective_time"' in module
    assert 'INPUT_CONTRACT_VERSION = "aidy_historical_replay_input_v9_effective_time"' in module
    assert "rc.input_contract_version=:input_contract_version" in module
    assert "rd.replay_version=:replay_version" in module
    assert "c.input_contract_version=:input_contract_version" in module
    assert '_PREVIOUS_INPUT_CONTRACT_VERSION = "aidy_historical_replay_input_v8"' in module
    assert "_LOAD_PREVIOUS_CASES" in module
    assert "derived_from_previous_frozen_contract" in module
    assert "build_failure_self_critique_context" in module
    assert "load_replay_self_feedback" in module
    assert '"failure_self_critique_context"' in module
    assert '"action_calibration": annotation.action_calibration' in module


def test_materializer_uses_target_revision_edit_time_as_effective_signal_time() -> None:
    source = MODULE.read_text(encoding="utf-8")
    timing = source.split("_EFFECTIVE_SIGNAL_TIME = text(", 1)[1].split(
        "_FORBIDDEN_INPUT_KEYS", 1
    )[0]
    assert "COALESCE(mr.edited_at,d.signal_posted_at) AS effective_signal_posted_at" in timing
    assert "mr.revision_index=o.revision_index" in timing
    materialize = source.split("    def materialize(", 1)[1].split(
        "    def _selected_cases", 1
    )[0]
    assert "effective_at < original_at" in materialize
    assert "historical_replay_revision_edit_time_missing" in materialize
    assert 'payload["signal_posted_at"] = effective_at.isoformat()' in materialize
    assert '"revision_edit_time_when_edited_else_original_post_time"' in materialize
    assert 'pit["target_revision_available_by_effective_signal_time"]' in materialize
    assert '"partition": _partition(effective_at)' in materialize


def test_effective_time_materializer_reuses_frozen_cohort_without_future_outcomes() -> None:
    source = MODULE.read_text(encoding="utf-8")
    previous = source.split("_LOAD_PREVIOUS_CASES = text(", 1)[1].split(
        "_EFFECTIVE_SIGNAL_TIME", 1
    )[0]
    assert "FROM aidy_historical_replay_cases" in previous
    assert "input_contract_version=:previous_input_contract_version" in previous
    assert "source_decision_id,source_id,signal_posted_at,partition,input_payload" in previous
    for forbidden in (
        "actual_pnl_usd",
        "actual_realized_r",
        "outcome_resolved_at",
        "resolution",
    ):
        assert forbidden not in previous
    materialize = source.split("    def materialize(", 1)[1].split(
        "    def _selected_cases", 1
    )[0]
    assert "_EFFECTIVE_SIGNAL_TIME" in materialize
    assert "build_event_liquidity_execution_context(" in materialize
    assert "build_probability_ev_management_context(" in materialize
    assert "build_failure_self_critique_context" in materialize
    assert "load_replay_self_feedback" in materialize
    assert 'payload["event_liquidity_execution_context"] = build2' in materialize
    assert 'payload["probability_ev_management_context"] = build4' in materialize
    assert 'payload["failure_self_critique_context"]' in materialize
    assert "load_provider_alpha_analogue_context" not in materialize


class _ReplayRetryEngine:
    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        self.calls = 0
        self.contexts = []
        self.annotation = object()

    async def reason(self, context):  # noqa: ANN001, ANN201 - tiny test double
        self.calls += 1
        self.contexts.append(context)
        if self.errors:
            raise AidyReasoningUnavailable(self.errors.pop(0))
        return self.annotation


def test_replay_retries_exact_provider_claim_validation_once() -> None:
    engine = _ReplayRetryEngine(["aidy_reasoning_provider_claim_invalid"])
    annotation, retries = asyncio.run(
        _reason_with_provider_claim_retry(
            engine,
            _signal_context_from_payload(_payload()),
        )
    )
    assert annotation is engine.annotation
    assert retries == 1
    assert engine.calls == 2
    assert engine.contexts[0].provider_evidence_claims
    assert engine.contexts[1].provider_evidence_claims == []
    assert engine.contexts[1].provider_intelligence is None
    assert engine.contexts[1].provider_profile is None


def test_replay_does_not_retry_other_reasoning_failures() -> None:
    engine = _ReplayRetryEngine(["aidy_reasoning_unavailable"])
    with pytest.raises(AidyReasoningUnavailable, match="aidy_reasoning_unavailable"):
        asyncio.run(
            _reason_with_provider_claim_retry(
                engine,
                _signal_context_from_payload(_payload()),
            )
        )
    assert engine.calls == 1


def test_replay_bounds_repeated_provider_claim_failure_to_one_retry() -> None:
    engine = _ReplayRetryEngine(
        [
            "aidy_reasoning_provider_claim_invalid",
            "aidy_reasoning_provider_claim_invalid",
        ]
    )
    with pytest.raises(
        AidyReasoningUnavailable,
        match="aidy_reasoning_provider_claim_invalid",
    ):
        asyncio.run(
            _reason_with_provider_claim_retry(
                engine,
                _signal_context_from_payload(_payload()),
            )
        )
    assert engine.calls == 2


class _ReplayToolAwareRetryEngine:
    def __init__(self) -> None:
        self.calls = 0
        self.kwargs = []
        self.annotation = object()

    async def reason(self, context, **kwargs):  # noqa: ANN001, ANN201
        self.calls += 1
        self.kwargs.append(kwargs)
        if self.calls == 1:
            raise AidyReasoningUnavailable("aidy_reasoning_provider_claim_invalid")
        return self.annotation


def test_provider_claim_retry_preserves_supplied_tools() -> None:
    engine = _ReplayToolAwareRetryEngine()

    async def executor(name, arguments):  # noqa: ANN001, ANN202
        return {"name": name, "arguments": arguments}

    annotation, retries = asyncio.run(
        _reason_with_provider_claim_retry(
            engine,
            _signal_context_from_payload(_payload()),
            tool_executor=executor,
            tool_schemas=[{"type": "function", "name": "example"}],
        )
    )
    assert annotation is engine.annotation
    assert retries == 1
    assert engine.calls == 2
    assert engine.kwargs[0]["tool_executor"] is executor
    assert engine.kwargs[1]["tool_executor"] is executor
    assert engine.kwargs[0]["tool_schemas"] == [{"type": "function", "name": "example"}]
    assert engine.kwargs[1]["tool_schemas"] == [{"type": "function", "name": "example"}]
