from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import MethodType, SimpleNamespace

from app.ai_message_supervisor import AiMessageDecision
from app.day27_management_policy import extract_day27_management_actions
import app.literal_management_overrides as overrides
from app.literal_management_overrides import explicit_literal_management
from app.mt5_execution_day26 import _SignalInput
from app.paper_execution_priority import PaperExecutionPriorityService
from app.telegram_entity_recovery import _canonical_channel_id
from app.telegram_listener_day38 import PaperPendingAwareListenerManager
from app.v1_message_policy import apply_v1_message_policy


CLOSE_ALL = {"type": "close", "target": "all", "value": None}
MOVE_BE = {"type": "move_to_break_even", "target": "all", "value": None}


def _decision(kind: str = "chatter", action: str = "ignore") -> AiMessageDecision:
    return AiMessageDecision(
        decision=kind,
        action=action,
        confidence=0.9,
        reason="model_classification",
        extracted={},
        model="test",
        response_id=None,
        latency_ms=1,
        source="openai",
        raw_text_sha256="x",
    )


def test_telethon_marked_and_raw_channel_ids_are_equivalent() -> None:
    assert _canonical_channel_id(-1002176701424) == 2176701424
    assert _canonical_channel_id(2176701424) == 2176701424


def test_tdc_out_this_setup_is_a_full_close() -> None:
    result = explicit_literal_management(
        "Out this set up at breakeven",
        fallback=extract_day27_management_actions,
    )
    assert CLOSE_ALL in result.actions


def test_tdc_we_are_out_after_tp1_is_a_full_close() -> None:
    result = explicit_literal_management(
        "We're out of this set up after we secured TP1",
        fallback=extract_day27_management_actions,
    )
    assert CLOSE_ALL in result.actions


def test_explicit_close_now_survives_later_if_you_wish_clause() -> None:
    result = explicit_literal_management(
        "Let's CLOSE our trade now and set breakeven if you wish to hold now",
        fallback=extract_day27_management_actions,
    )
    assert result.actions == (CLOSE_ALL,)


def test_true_close_or_be_choice_remains_protective_not_forced_exit() -> None:
    result = explicit_literal_management(
        "Let's CLOSE our trade now, or set breakeven if you plan to hold on",
        fallback=extract_day27_management_actions,
    )
    assert MOVE_BE in result.actions
    assert CLOSE_ALL not in result.actions


def test_literal_management_is_promoted_even_when_ai_called_it_chatter(monkeypatch) -> None:
    monkeypatch.setattr(overrides, "_original", extract_day27_management_actions)
    result = overrides._promote_literal_management(
        _decision(),
        raw_text="Move gold stoploss to 4375",
        original=apply_v1_message_policy,
    )
    assert result.decision == "trade_update"
    assert result.action == "apply_update"
    assert result.extracted["management_actions"] == [
        {"type": "edit_stop_loss", "target": "all", "value": "4375"}
    ]


def test_owner_demo_one_percent_is_total_signal_risk_not_per_tp() -> None:
    service = object.__new__(PaperExecutionPriorityService)
    signal = _SignalInput(
        signal_id=SimpleNamespace(),  # type: ignore[arg-type]
        symbol="XAUUSD",
        side="BUY",
        entry_low=Decimal("2000"),
        entry_high=Decimal("2000"),
        stop_loss=Decimal("1990"),
        take_profits=(Decimal("2010"), Decimal("2020"), Decimal("2030")),
        has_open_runner=False,
        signal_requests_double_lot=False,
        source_revision_index=0,
        source_posted_at=datetime.now(UTC),
    )
    sizing = service._size_signal(
        signal=signal,
        execution_entry=Decimal("2000"),
        balance=1000.0,
        price_loss_tick_value=1.0,
        specification={"minVolume": "0.001", "maxVolume": "100", "volumeStep": "0.001", "tickSize": "1"},
        risk_percent="1",
        double_lot_approved=False,
    )
    assert sizing.position_count == 3
    assert sizing.total_actual_risk <= Decimal("10")
    assert sizing.total_actual_risk > Decimal("9")


class _StoredRouter:
    def __init__(self, decision: str, action: str) -> None:
        self.stored = SimpleNamespace(decision=decision, action=action)

    def _load_stored_decision(self, **kwargs):
        return self.stored


def _recovery_manager(decision: str, action: str):
    manager = object.__new__(PaperPendingAwareListenerManager)
    manager._day28_router = _StoredRouter(decision, action)
    calls: list[dict] = []

    def dispatch(self, **kwargs):
        calls.append(dict(kwargs))

    manager._dispatch_sync = MethodType(dispatch, manager)
    return manager, calls


def test_restart_recovery_dispatches_stale_management() -> None:
    manager, calls = _recovery_manager("trade_update", "apply_update")
    asyncio.run(
        manager._dispatch_recovered_if_required(
            source_id=SimpleNamespace(),  # type: ignore[arg-type]
            telegram_message_id=123,
            revision_index=0,
            occurred_at=datetime.now(UTC) - timedelta(hours=3),
        )
    )
    assert len(calls) == 1


def test_restart_recovery_does_not_execute_stale_new_trade() -> None:
    manager, calls = _recovery_manager("new_trade", "execute")
    asyncio.run(
        manager._dispatch_recovered_if_required(
            source_id=SimpleNamespace(),  # type: ignore[arg-type]
            telegram_message_id=124,
            revision_index=0,
            occurred_at=datetime.now(UTC) - timedelta(hours=3),
        )
    )
    assert calls == []
