from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from app import telegram_publisher_day34 as module
from app.telegram_publisher_day34 import Day34TelegramPublisherManager


class _Result:
    def __init__(self, row: dict[str, Any]) -> None:
        self._row = row

    def mappings(self) -> "_Result":
        return self

    def one(self) -> dict[str, Any]:
        return self._row


class _Session:
    def __init__(self, row: dict[str, Any]) -> None:
        self._row = row

    def __enter__(self) -> "_Session":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def execute(self, *_: Any, **__: Any) -> _Result:
        return _Result(self._row)


def _manager(state: dict[str, Any], chat_id: str, monkeypatch: Any) -> tuple[Any, list[tuple[str, dict]]]:
    manager = object.__new__(Day34TelegramPublisherManager)
    manager._bot_token = "token"
    manager._destination_chat_id = chat_id
    manager._session_factory = lambda: _Session(state)
    manager._live_board_rows = lambda: []
    manager._render_live_board = lambda rows: "board"
    manager._mark_board_attempt = lambda: None
    calls: list[tuple[str, dict]] = []
    recorded: list[tuple] = []

    def fake_api(token: str, method: str, payload: dict) -> dict:
        calls.append((method, payload))
        return {"message_id": 777}

    monkeypatch.setattr(module, "_bot_api_call", fake_api)
    manager._record_board_message = lambda *a, **k: recorded.append(("message", a, k))
    manager._record_board_pinned = lambda message_id: recorded.append(("pinned", message_id))
    return manager, calls


def _state(chat_id: str) -> dict[str, Any]:
    return {
        "telegram_message_id": 3586,
        "destination_chat_id": chat_id,
        "source_digest": "stale",
        "pinned_at": datetime(2026, 8, 12, tzinfo=timezone.utc),
    }


def test_board_is_reposted_and_pinned_when_destination_chat_changes(monkeypatch) -> None:
    manager, calls = _manager(_state("-5314636936"), "-1004402233886", monkeypatch)
    manager._sync_live_board()
    assert [method for method, _ in calls] == ["sendMessage", "pinChatMessage"]
    assert calls[0][1]["chat_id"] == "-1004402233886"
    assert calls[1][1]["message_id"] == 777


def test_board_is_edited_in_place_when_destination_chat_is_unchanged(monkeypatch) -> None:
    manager, calls = _manager(_state("-1004402233886"), "-1004402233886", monkeypatch)
    manager._sync_live_board()
    assert [method for method, _ in calls] == ["editMessageText"]
    assert calls[0][1]["message_id"] == 3586
