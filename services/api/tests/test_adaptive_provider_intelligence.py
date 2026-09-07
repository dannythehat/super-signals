from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from app.benchmarking_execution_dispatch import BenchmarkingCanonicalExecutionDispatcher
from app.provider_adaptive_profile import AdaptiveProviderProfileService, _masked


def test_adaptive_profile_learns_provider_grammar_without_historical_numbers() -> None:
    now = datetime.now(timezone.utc)
    decisions = [
        {
            "decision": "new_trade",
            "action": "execute",
            "revision_index": 1,
            "raw_text": "BUY GOLD 4385-4376 TP1 4390 TP2 4400 SL 4371 second entry",
        },
        {
            "decision": "trade_update",
            "action": "apply_update",
            "revision_index": 0,
            "raw_text": "TP1 hit - move SL to BE and leave runner open",
        },
        {
            "decision": "preparation",
            "action": "ignore",
            "revision_index": 0,
            "raw_text": "Get ready for gold",
        },
    ]
    signals = [
        {
            "symbol": "XAUUSD",
            "side": "BUY",
            "order_type": "pending",
            "entry_low": Decimal("4376"),
            "entry_high": Decimal("4385"),
            "take_profits": ["4390", "4400"],
            "has_open_runner": True,
            "source_revision_index": 1,
            "source_posted_at": now,
            "original_text": "BUY GOLD 4385-4376 second entry runner",
        }
    ]

    payload = AdaptiveProviderProfileService._profile_payload(
        source_id=uuid4(),
        source={"title": "Gold Test Provider", "status": "shadow"},
        decisions=decisions,
        signals=signals,
        broker_outcomes=[],
        shadow_outcomes=[],
    )
    language = payload["language"]
    assert language["instrument_bucket"] == "xauusd_dedicated"
    assert language["entry_bucket"] == "zone"
    assert language["order_bucket"] == "pending"
    assert language["sequence_bucket"] == "edit_completed_setup"
    assert language["management_bucket"] == "active_management"
    assert language["traits"]["uses_layered_entries"] is True
    assert language["traits"]["uses_runner_language"] is True
    assert language["traits"]["uses_breakeven_language"] is True
    examples = " ".join(language["grammar_examples_masked"]["new_trade"])
    assert "4385" not in examples
    assert "4376" not in examples
    assert "<N>" in examples


def test_performance_profile_flags_fast_win_slow_loss_pattern_only_with_evidence() -> None:
    now = datetime.now(timezone.utc)
    outcomes = []
    for index in range(12):
        outcomes.append(
            {
                "side": "BUY",
                "status": "won",
                "opened_at": now,
                "closed_at": now + timedelta(minutes=5 + index % 4),
                "cash_pnl": Decimal("10"),
            }
        )
    for index in range(8):
        outcomes.append(
            {
                "side": "BUY",
                "status": "lost",
                "opened_at": now,
                "closed_at": now + timedelta(minutes=25 + index),
                "cash_pnl": Decimal("-10"),
            }
        )

    profile = AdaptiveProviderProfileService._performance_profile(outcomes, [])
    assert profile["closed_outcomes"] == 20
    assert profile["stale_trade_pattern"]["status"] == "candidate"
    assert profile["stale_trade_pattern"]["watch_after_minutes"] >= 5
    assert profile["stale_trade_pattern"]["auto_apply"] is False


def test_masking_removes_urls_and_numeric_execution_values() -> None:
    masked = _masked("BUY GOLD 4385-4376 TP1 4390 SL 4371 https://example.com/x")
    assert "4385" not in masked
    assert "4371" not in masked
    assert "https://" not in masked
    assert "<N>" in masked
    assert "<URL>" in masked


class _Shadow:
    def __init__(self) -> None:
        self.signal_ids = []
        self.event_ids = []

    def record_signal(self, signal_id):
        self.signal_ids.append(signal_id)
        return True

    def record_management(self, event_id):
        self.event_ids.append(event_id)
        return True


def test_benchmark_sidecar_records_testing_instruction_without_broker_dependency() -> None:
    dispatcher = object.__new__(BenchmarkingCanonicalExecutionDispatcher)
    dispatcher._shadow = _Shadow()
    signal_id = uuid4()
    event_id = uuid4()
    dispatcher._resolve_signal_id = lambda _message_id, _revision: signal_id
    dispatcher._resolve_lifecycle_event = lambda _message_id, _revision: (event_id, signal_id)

    trade = SimpleNamespace(message_id=uuid4(), decision="new_trade", action="execute")
    dispatcher._record_benchmark_sidecar(trade, 0)
    assert dispatcher._shadow.signal_ids == [signal_id]

    update = SimpleNamespace(message_id=uuid4(), decision="trade_update", action="apply_update")
    dispatcher._record_benchmark_sidecar(update, 0)
    assert dispatcher._shadow.event_ids == [event_id]
