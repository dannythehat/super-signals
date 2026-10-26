"""Regression: no internal AI invocation quotas block legitimate Telegram processing."""
import inspect

from app import ai_message_supervisor_day34 as day34


def test_ai_supervisor_has_no_internal_daily_monthly_or_shadow_budget_gate():
    source = inspect.getsource(day34.Day34OpenAiMessageSupervisor.decide_with_active_context)
    assert "_paid_ai_budget_reason" not in source
    assert "_budget_skip" not in source
    assert not hasattr(day34, "_PAID_AI_DAILY_LIMIT")
    assert not hasattr(day34, "_PAID_AI_MONTHLY_LIMIT")
    assert not hasattr(day34, "_PAID_AI_SHADOW_DAILY_LIMIT")
    assert not hasattr(day34, "_PAID_AI_SHADOW_MONTHLY_LIMIT")


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
