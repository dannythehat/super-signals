from app import ai_message_supervisor_day34 as day34


class _FakeMappingsResult:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def one(self):
        return self._row


class _FakeSession:
    def __init__(self, row):
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, _statement):
        return _FakeMappingsResult(self._row)


def _factory_for(row):
    return lambda: _FakeSession(row)


def test_paid_ai_budget_allows_normal_live_call_below_limits(monkeypatch):
    monkeypatch.setattr(
        day34,
        "get_session_factory",
        lambda: _factory_for(
            {"day_total": 10, "month_total": 100, "day_shadow": 3, "month_shadow": 20}
        ),
    )
    assert day34._paid_ai_budget_reason("testing") is None


def test_paid_ai_budget_blocks_total_daily_limit(monkeypatch):
    monkeypatch.setattr(
        day34,
        "get_session_factory",
        lambda: _factory_for(
            {
                "day_total": day34._PAID_AI_DAILY_LIMIT,
                "month_total": 100,
                "day_shadow": 0,
                "month_shadow": 0,
            }
        ),
    )
    assert day34._paid_ai_budget_reason("testing") == "paid_ai_daily_limit_reached"


def test_paid_ai_budget_blocks_shadow_before_live_allowance(monkeypatch):
    monkeypatch.setattr(
        day34,
        "get_session_factory",
        lambda: _factory_for(
            {
                "day_total": 40,
                "month_total": 300,
                "day_shadow": day34._PAID_AI_SHADOW_DAILY_LIMIT,
                "month_shadow": 100,
            }
        ),
    )
    assert day34._paid_ai_budget_reason("shadow") == "paid_ai_shadow_daily_limit_reached"
    assert day34._paid_ai_budget_reason("testing") is None


def test_budget_accounting_failure_fails_closed(monkeypatch):
    def _broken_factory():
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(day34, "get_session_factory", _broken_factory)
    assert day34._paid_ai_budget_reason("testing") == "paid_ai_budget_guard_unavailable"


def test_exhausted_budget_never_calls_openai(monkeypatch):
    monkeypatch.setenv("SUPER_SIGNALS_ENV", "production")
    monkeypatch.setattr(day34, "_paid_ai_budget_reason", lambda _status: "paid_ai_daily_limit_reached")

    def _must_not_call(*_args, **_kwargs):
        raise AssertionError("OpenAI HTTP call must not happen after budget exhaustion")

    monkeypatch.setattr(day34.httpx, "post", _must_not_call)
    supervisor = day34.Day34OpenAiMessageSupervisor(api_key="test-key", model="gpt-5-mini")
    result = supervisor.decide_with_active_context(
        raw_text="I'm buying 4143",
        source_status="testing",
        source_name="The Gold Club - TGC",
        active_trade_context=[],
        recent_source_messages=[],
    )
    assert result.action == "skip"
    assert result.source == "deterministic_no_ai"
    assert result.reason == "paid_ai_daily_limit_reached"


def test_paid_ai_output_cap_is_bounded():
    assert day34._MAX_OUTPUT_TOKENS <= 500


def test_recent_context_is_compacted():
    messages = [
        {"telegram_message_id": index, "text": "x" * 1000}
        for index in range(day34._RECENT_CONTEXT_LIMIT + 5)
    ]
    compact = day34._compact_recent_context(messages)
    assert len(compact) == day34._RECENT_CONTEXT_LIMIT
    assert compact[0]["telegram_message_id"] == 5
    assert all(len(item["text"]) <= day34._RECENT_CONTEXT_TEXT_LIMIT for item in compact)


def test_active_lifecycle_context_is_compacted():
    active = [
        {
            "signal_id": str(index),
            "recent_lifecycle": [
                {"event_type": "update", "rendered_text": "y" * 1000}
                for _ in range(day34._ACTIVE_LIFECYCLE_LIMIT + 4)
            ],
        }
        for index in range(day34._ACTIVE_CONTEXT_LIMIT + 4)
    ]
    compact = day34._compact_active_context(active)
    assert len(compact) == day34._ACTIVE_CONTEXT_LIMIT
    for item in compact:
        assert len(item["recent_lifecycle"]) == day34._ACTIVE_LIFECYCLE_LIMIT
        assert all(
            len(event["rendered_text"]) <= day34._ACTIVE_LIFECYCLE_TEXT_LIMIT
            for event in item["recent_lifecycle"]
        )
