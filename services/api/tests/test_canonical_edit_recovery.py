from datetime import UTC, datetime
from threading import RLock
from types import MethodType
from uuid import uuid4

from app.telegram_listener_canonical import CanonicalProductionTelegramListenerManager
from app.telegram_listener_day13 import CapturedTelegramEdit, Day13TelegramListenerManager


def _captured() -> CapturedTelegramEdit:
    return CapturedTelegramEdit(
        source_id=uuid4(),
        chat_id=-100123456,
        telegram_message_id=7001,
        raw_text="BUY XAUUSD 4400\nSL 4390\nTP 4410",
        edited_at=datetime(2026, 8, 19, 18, 0, tzinfo=UTC),
        reply_to_message_id=None,
        has_media=False,
        media_type=None,
    )


def _manager() -> CanonicalProductionTelegramListenerManager:
    manager = object.__new__(CanonicalProductionTelegramListenerManager)
    manager._telegram_revision_locks = tuple(RLock() for _ in range(128))
    manager._ai_pipeline = None
    return manager


def test_recovered_existing_edit_resolves_exact_revision_without_removed_helper(monkeypatch) -> None:
    manager = _manager()
    captured = _captured()
    monkeypatch.setattr(Day13TelegramListenerManager, "_persist_edit", lambda _self, _captured: False)
    manager._exact_saved_revision_index = MethodType(lambda _self, _captured: 6, manager)

    inserted, revision_index = manager._persist_recovered_edit(captured)

    assert inserted is False
    assert revision_index == 6
    assert not hasattr(manager, "_latest_revision_index")


def test_recovered_new_edit_processes_the_exact_saved_revision_once(monkeypatch) -> None:
    manager = _manager()
    captured = _captured()
    processed: list[tuple[object, int, int]] = []

    class Pipeline:
        def _process_revision(self, source_id, telegram_message_id, *, revision_index):
            processed.append((source_id, telegram_message_id, revision_index))

    manager._ai_pipeline = Pipeline()
    monkeypatch.setattr(Day13TelegramListenerManager, "_persist_edit", lambda _self, _captured: True)
    manager._exact_saved_revision_index = MethodType(lambda _self, _captured: 4, manager)

    inserted, revision_index = manager._persist_recovered_edit(captured)

    assert inserted is True
    assert revision_index == 4
    assert processed == [(captured.source_id, captured.telegram_message_id, 4)]
