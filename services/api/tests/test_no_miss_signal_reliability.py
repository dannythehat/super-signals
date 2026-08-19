from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import MethodType, SimpleNamespace

from app.ai_message_pipeline_canonical import explicit_management_without_ai
from app.day27_management_policy import extract_day27_management_actions
from app.telegram_entity_recovery import _canonical_channel_id
from app.telegram_listener_canonical import CanonicalProductionTelegramListenerManager


CLOSE_ALL = {"type": "close", "target": "all", "value": None}
MOVE_BE = {"type": "move_to_break_even", "target": "all", "value": None}


def test_telethon_marked_and_raw_channel_ids_are_equivalent() -> None:
    assert _canonical_channel_id(-1002176701424) == 2176701424
    assert _canonical_channel_id(2176701424) == 2176701424


def test_tdc_out_this_setup_is_a_full_close() -> None:
    result = extract_day27_management_actions("Out this set up at breakeven")
    assert CLOSE_ALL in result.actions


def test_tdc_we_are_out_after_tp1_is_a_full_close() -> None:
    result = extract_day27_management_actions("We're out of this set up after we secured TP1")
    assert CLOSE_ALL in result.actions


def test_explicit_close_now_survives_later_if_you_wish_clause() -> None:
    result = extract_day27_management_actions(
        "Let's CLOSE our trade now and set breakeven if you wish to hold now"
    )
    assert CLOSE_ALL in result.actions


def test_true_close_or_be_choice_remains_protective_not_forced_exit() -> None:
    result = extract_day27_management_actions(
        "Let's CLOSE our trade now, or set breakeven if you plan to hold on"
    )
    assert MOVE_BE in result.actions
    assert CLOSE_ALL not in result.actions


def test_fxtradingvision_plural_gold_stoplosses_are_actionable() -> None:
    result = extract_day27_management_actions(
        "Move all gold stoplosses to 4405 to be safe from wicks."
    )
    assert {"type": "edit_stop_loss", "target": "all", "value": "4405"} in result.actions


def test_fxtradingvision_plural_stoplosses_without_all_are_actionable() -> None:
    result = extract_day27_management_actions("Move gold stoplosses to 4395.")
    assert {"type": "edit_stop_loss", "target": "all", "value": "4395"} in result.actions


def test_tdc_risk_free_typo_with_explicit_price_is_actionable() -> None:
    result = extract_day27_management_actions("+20\n\nRISK FREEE 4393")
    assert {"type": "edit_stop_loss", "target": "all", "value": "4393"} in result.actions


def test_literal_management_bypasses_ai_chatter_classification_entirely() -> None:
    decision = explicit_management_without_ai("Move gold stoploss to 4375")
    assert decision is not None
    assert decision.decision == "trade_update"
    assert decision.action == "apply_update"
    assert decision.extracted["management_actions"] == [
        {"type": "edit_stop_loss", "target": "all", "value": "4375"}
    ]


class _StoredRouter:
    def __init__(self, decision: str, action: str) -> None:
        self.stored = SimpleNamespace(
            message_id=SimpleNamespace(),
            decision=decision,
            action=action,
        )

    def _load_stored_decision(self, **kwargs):
        return self.stored


def _recovery_manager(decision: str, action: str):
    manager = object.__new__(CanonicalProductionTelegramListenerManager)
    manager._canonical_router = _StoredRouter(decision, action)
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
