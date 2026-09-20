from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.aidy_historical_stress_lab import (
    STRESS_INPUT_CONTRACT_VERSION,
    STRESS_REPLAY_VERSION,
    AidyHistoricalStressLabService,
    _score_gold_view_path,
    _partition,
    _reconstructed_provider_claims,
    _scope_from_env,
    build_reconstructed_market_context,
)
from app.aidy_market_client import AidyM1Bar


def _bar(minute: int, close: str, *, high: str | None = None, low: str | None = None) -> AidyM1Bar:
    opened = datetime(2026, 8, 20, 8, 0, tzinfo=UTC) + timedelta(minutes=minute)
    value = Decimal(close)
    return AidyM1Bar(
        open_time_utc=opened,
        open=value,
        high=Decimal(high) if high is not None else value + Decimal("0.4"),
        low=Decimal(low) if low is not None else value - Decimal("0.4"),
        close=value,
        revision_index=0,
        first_observed_at=opened + timedelta(days=30),
        payload_digest="a" * 64,
    )


def test_stress_lab_versions_are_separate_from_exact_pit_replay() -> None:
    assert STRESS_REPLAY_VERSION == "aidy_historical_stress_lab_v9_gold_first"
    assert STRESS_INPUT_CONTRACT_VERSION == "aidy_historical_stress_input_v5_toolbox"


def test_stress_partition_is_chronological() -> None:
    assert _partition(datetime(2026, 9, 3, 23, 59, tzinfo=UTC)) == "research_train"
    assert _partition(datetime(2026, 9, 4, 12, 0, tzinfo=UTC)) == "research_validation"
    assert _partition(datetime(2026, 9, 6, 12, 0, tzinfo=UTC)) == "research_oos"


def test_reconstructed_market_never_claims_exact_pit() -> None:
    bars = [_bar(i, str(4000 + i * 0.1)) for i in range(240)]
    signal_at = datetime(2026, 8, 20, 12, 0, 30, tzinfo=UTC)
    packet = build_reconstructed_market_context(signal_posted_at=signal_at, bars=bars)

    assert packet["reconstruction_tier"] == "retrospective_research_m1"
    assert packet["pit_eligible"] is False
    assert packet["decision_admitted"] is False
    assert packet["reconstruction_provenance"]["future_bars_in_model_input"] is False
    assert packet["reconstruction_provenance"]["exact_pit_claimed"] is False
    assert packet["research_only"] is True
    assert packet["live_money_execution_allowed"] is False


def test_reconstructed_market_excludes_bar_opening_at_signal_minute() -> None:
    bars = [_bar(i, "4000") for i in range(240)]
    bars.append(_bar(240, "9999"))
    signal_at = datetime(2026, 8, 20, 12, 0, 30, tzinfo=UTC)
    packet = build_reconstructed_market_context(signal_posted_at=signal_at, bars=bars)
    assert packet["market"]["quote_context"]["mid"] == "4000"


def test_stress_module_keeps_official_holdout_separate() -> None:
    import inspect
    import app.aidy_historical_stress_lab as module

    source = inspect.getsource(module)
    assert "official_18_case_holdout_excluded" in source
    assert "reconstructed_research" in source
    assert "AIDY_HISTORICAL_STRESS_ENABLED" in source
    assert "aidy_historical_time_machine_v9" not in source


def test_stress_default_capacity_covers_entire_scoreable_cohort() -> None:
    import inspect
    import app.aidy_historical_stress_lab as module

    source = inspect.getsource(module)
    assert "_SOURCE_UNIVERSE_WITH_DECISION_OUTCOMES = 803" in source
    assert "_EXPECTED_COHORT = 800" in source
    assert "_DEFAULT_MAX_CALLS = 800" in source
    assert '"research_train": 571' in source
    assert '"research_validation": 69' in source
    assert '"research_oos": 160' in source
    assert "_EXPECTED_COHORT_SHA256" in source
    assert "historical_stress_candidate_identity_changed" in source
    assert "_EXPECTED_TRAIN_COHORT = 571" in source
    assert '_EXPECTED_TRAIN_SHA256 = "18326c515d12a7e55046828f6fe59198de50b0b7538193174528bf56f7829168"' in source
    assert 'if self._scope == "train"' in source
    assert "historical_stress_train_candidate_identity_changed" in source


def test_validation_and_oos_are_separately_locked(monkeypatch) -> None:
    monkeypatch.setenv("AIDY_HISTORICAL_STRESS_SCOPE", "all")
    monkeypatch.delenv("AIDY_HISTORICAL_STRESS_OPEN_VALIDATION", raising=False)
    monkeypatch.delenv("AIDY_HISTORICAL_STRESS_OPEN_OOS", raising=False)
    assert _scope_from_env() == "train"

    monkeypatch.setenv("AIDY_HISTORICAL_STRESS_OPEN_VALIDATION", "1")
    assert _scope_from_env() == "train_validation"

    monkeypatch.setenv("AIDY_HISTORICAL_STRESS_OPEN_OOS", "1")
    assert _scope_from_env() == "all"


def test_oos_flag_alone_does_not_open_validation(monkeypatch) -> None:
    monkeypatch.setenv("AIDY_HISTORICAL_STRESS_SCOPE", "all")
    monkeypatch.delenv("AIDY_HISTORICAL_STRESS_OPEN_VALIDATION", raising=False)
    monkeypatch.setenv("AIDY_HISTORICAL_STRESS_OPEN_OOS", "1")
    assert _scope_from_env() == "train"


def test_stress_reasoning_offers_research_candle_tool_not_live_pit_tool() -> None:
    import inspect
    import app.aidy_historical_stress_lab as module

    source = inspect.getsource(module)
    assert "fetch_research_m1" in source
    assert "HISTORICAL_CALENDAR_TOOL_SCHEMA" in source
    assert "HISTORICAL_EVIDENCE_TOOL_SCHEMA" in source
    assert '"pit_eligible": False' in source
    assert '"decision_admitted": False' in source
    reason_block = source.split("    async def reason(", 1)[1].split("    def score(", 1)[0]
    assert '"tools_offered": True' in reason_block
    assert '"historical_calendar": True' in reason_block
    assert '"focused_evidence_inspector": True' in reason_block
    assert '"toolbox_manifest": True' in reason_block
    assert '"tool_names_used": list(annotation.tool_names_used)' in reason_block


def test_stress_materializer_attaches_research_calendar_before_build2() -> None:
    import inspect
    import app.aidy_historical_stress_lab as module

    source = inspect.getsource(module)
    materialize = source.split("    async def materialize(", 1)[1].split(
        "    def _selected_cases", 1
    )[0]
    assert "attach_historical_schedule(" in materialize
    assert "calendar_evidence_tier" in materialize
    assert "retrospective_calendar_source_explicitly_tagged" in materialize
    assert '"toolbox_manifest": historical_toolbox_manifest(payload)' in materialize


def test_reconstructed_provider_claims_use_only_prior_known_results() -> None:
    source = "provider-1"
    target = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    pool = []
    for index in range(8):
        pool.append(
            {
                "source_id": source,
                "side": "BUY",
                "signal_posted_at": target - timedelta(days=9 - index),
                "prior_result_known_at": target - timedelta(days=8 - index),
                "net_pnl_usd": Decimal("10") if index < 6 else Decimal("-5"),
            }
        )
    pool.append(
        {
            "source_id": source,
            "side": "BUY",
            "signal_posted_at": target - timedelta(hours=1),
            "prior_result_known_at": target + timedelta(hours=1),
            "net_pnl_usd": Decimal("9999"),
        }
    )

    claims = _reconstructed_provider_claims(
        pool,
        source_id=source,
        side="BUY",
        signal_posted_at=target,
    )
    overall = next(item for item in claims if item["id"] == "provider.performance.overall")
    assert overall["sample_n"] == 8
    assert overall["value"]["wins"] == 6
    assert overall["value"]["losses"] == 2
    assert overall["value"]["net_pnl_usd"] == "50"
    assert overall["as_of_utc"] == target.isoformat()


def test_stress_reasoning_uses_bounded_provider_retry_with_candle_tools() -> None:
    import inspect
    import app.aidy_historical_stress_lab as module

    source = inspect.getsource(module)
    reason_block = source.split("    async def reason(", 1)[1].split("    def score(", 1)[0]
    assert "_reason_with_provider_claim_retry(" in reason_block
    assert "tool_executor=self._stress_tool_executor(" in reason_block
    assert "HISTORICAL_CALENDAR_TOOL_SCHEMA" in reason_block
    assert "HISTORICAL_EVIDENCE_TOOL_SCHEMA" in reason_block


def test_stress_runtime_defers_heavy_work_until_after_startup_grace() -> None:
    import inspect
    import app.aidy_historical_stress_lab as module

    source = inspect.getsource(module.AidyHistoricalStressLabRuntime)
    assert "AIDY_HISTORICAL_STRESS_STARTUP_DELAY_SECONDS" in source
    run_block = source.split("    async def _run(", 1)[1]
    assert "timeout=self._startup_delay_seconds" in run_block
    assert run_block.index("timeout=self._startup_delay_seconds") < run_block.index(
        "service.run_once("
    )


def test_stress_uses_revision_edit_time_as_effective_signal_time() -> None:
    import inspect
    import app.aidy_historical_stress_lab as module

    source = inspect.getsource(module)
    candidate_sql = str(module._CANDIDATES)
    prior_sql = str(module._PRIOR_POOL)
    assert "COALESCE(target_rev.edited_at,d.signal_posted_at) AS signal_posted_at" in candidate_sql
    assert "target_rev.revision_index=o.revision_index" in candidate_sql
    assert "target_rev.revision_index=o.revision_index" in prior_sql
    assert "signal_time_semantics" in source
    assert "target_revision_available_by_signal_time" in source


def test_historical_preflight_router_forces_structure_evidence_when_context_is_unclear() -> None:
    payload = {
        "market_context": {
            "trend_structure": "unknown",
            "volatility_band": "unknown",
            "event_timing": "verified_no_nearby_high_impact_event",
        },
        "event_liquidity_execution_context": {
            "event": {"nearest_scheduled_event": None},
        },
        "provider_evidence_claims": [],
    }
    plan = AidyHistoricalStressLabService._historical_preflight_plan(payload)
    names = [item["name"] for item in plan]
    candle_args = [
        item["arguments"]
        for item in plan
        if item["name"] == "get_recent_candles"
    ]
    assert names.count("get_recent_candles") == 2
    assert {"timeframe_minutes": 15, "lookback_count": 8} in candle_args
    assert {"timeframe_minutes": 60, "lookback_count": 6} in candle_args


def test_historical_preflight_router_focuses_event_and_provider_tools_when_material() -> None:
    payload = {
        "market_context": {
            "trend_structure": "bullish_trend",
            "volatility_band": "normal",
            "event_timing": "verified_high_impact_event_same_utc_day",
        },
        "event_liquidity_execution_context": {
            "event": {
                "nearest_scheduled_event": {
                    "minutes_from_signal": 120,
                }
            }
        },
        "provider_evidence_claims": [
            {"id": "provider.performance.overall", "value": {"sample_n": 12}}
        ],
    }
    plan = AidyHistoricalStressLabService._historical_preflight_plan(payload)
    names = [item["name"] for item in plan]
    assert names.count("get_recent_candles") == 1
    assert "get_economic_calendar" in names
    assert "inspect_historical_evidence" in names


def test_historical_v8_persists_preflight_tool_telemetry_and_valid_run_scope() -> None:
    import inspect
    import app.aidy_historical_stress_lab as module

    source = inspect.getsource(module)
    reason_block = source.split("    async def reason(", 1)[1].split("    def score(", 1)[0]
    run_block = source.split("    async def run_once(", 1)[1].split(
        "\n\nclass AidyHistoricalStressLabRuntime", 1
    )[0]
    assert '"preflight_evidence_calls": annotation.preflight_evidence_calls' in reason_block
    assert '"preflight_tool_names": list(preflight_tool_names)' in reason_block
    assert '"preflight_tool_evidence": preflight_evidence' in reason_block
    assert '"all_evidence_tool_names": sorted(' in reason_block
    assert '"partition_scope": self._scope' in run_block
    assert 'f"stress_{self._scope}"' not in run_block



def test_gold_first_stress_output_freezes_independent_market_view() -> None:
    source = MODULE.read_text(encoding="utf-8")
    decide = source.split("    async def decide(", 1)[1].split("    def score(", 1)[0]
    for field in (
        '"gold_view_direction": annotation.gold_view_direction',
        '"gold_view_confidence": annotation.gold_view_confidence',
        '"gold_view_horizon_minutes": annotation.gold_view_horizon_minutes',
        '"gold_view_reason": annotation.gold_view_reason',
        '"provider_alignment": annotation.provider_alignment',
    ):
        assert field in decide



def _path_bar(minute: int, *, open_: str, high: str, low: str, close: str) -> AidyM1Bar:
    opened = datetime(2026, 9, 1, 10, minute, tzinfo=UTC)
    return AidyM1Bar(
        open_time_utc=opened,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        revision_index=0,
        first_observed_at=opened + timedelta(minutes=1),
        payload_digest="a" * 64,
    )


def test_gold_view_path_scorer_credits_correct_opposite_direction_view() -> None:
    bars = [
        _path_bar(1, open_="4400", high="4402", low="4395", close="4397"),
        _path_bar(2, open_="4397", high="4398", low="4388", close="4390"),
        _path_bar(3, open_="4390", high="4392", low="4380", close="4382"),
    ]

    bearish = _score_gold_view_path(direction="bearish", bars=bars)
    bullish = _score_gold_view_path(direction="bullish", bars=bars)

    assert bearish is not None and bullish is not None
    assert bearish["outcome_class"] == "favorable"
    assert bearish["directional_move_points"] == Decimal("18")
    assert bearish["favorable_excursion_points"] == Decimal("20")
    assert bearish["adverse_excursion_points"] == Decimal("2")
    assert bullish["outcome_class"] == "adverse"
    assert bullish["directional_move_points"] == Decimal("-18")


def test_gold_view_path_scorer_does_not_invent_a_trade_for_neutral_or_unknown() -> None:
    bars = [_path_bar(1, open_="4400", high="4402", low="4398", close="4401")]
    assert _score_gold_view_path(direction="neutral", bars=bars) is None
    assert _score_gold_view_path(direction="unknown", bars=bars) is None
