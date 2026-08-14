from __future__ import annotations

from app.telegram_live_board_reconcile import force_reconcile_live_board
from app.telegram_publisher import TelegramPublishError


class _BoardHarness:
    def __init__(self) -> None:
        self._bot_token = "test-token"
        self._destination_chat_id = -123
        self.normal_sync_count = 0
        self.attempt_count = 0
        self.recorded_messages: list[tuple[int, str, str, bool]] = []
        self.recorded_pins: list[int] = []

    def _sync_live_board(self) -> None:
        self.normal_sync_count += 1

    def _live_board_rows(self):
        return [{"signal": "open"}]

    def _render_live_board(self, rows) -> str:
        assert rows == [{"signal": "open"}]
        return "📌 SUPER SIGNALS · LIVE TRADES\nOPEN 1 · PENDING 0"

    def _mark_board_attempt(self) -> None:
        self.attempt_count += 1

    def _record_board_message(
        self,
        message_id: int,
        rendered: str,
        digest: str,
        *,
        created: bool,
    ) -> None:
        self.recorded_messages.append((message_id, rendered, digest, created))

    def _record_board_pinned(self, message_id: int) -> None:
        self.recorded_pins.append(message_id)

    def _is_missing_board_message(self, exc: TelegramPublishError) -> bool:
        del exc
        return False

    def _replace_missing_live_board(self, rendered: str, digest: str) -> None:
        raise AssertionError(f"unexpected replacement {rendered} {digest}")


def test_force_reconcile_refreshes_and_repins_live_board(monkeypatch) -> None:
    publisher = _BoardHarness()
    methods: list[str] = []

    monkeypatch.setattr(
        "app.telegram_live_board_reconcile._current_live_board_message_id",
        lambda manager: 2,
    )

    def fake_bot_call(token, method, payload):
        assert token == "test-token"
        assert payload["chat_id"] == -123
        methods.append(method)
        return {"message_id": 2}

    monkeypatch.setattr("app.telegram_live_board_reconcile._bot_api_call", fake_bot_call)

    assert force_reconcile_live_board(publisher) is True
    assert publisher.normal_sync_count == 1
    assert publisher.attempt_count == 1
    assert methods == ["editMessageText", "unpinChatMessage", "pinChatMessage"]
    assert publisher.recorded_messages[0][0] == 2
    assert "OPEN 1 · PENDING 0" in publisher.recorded_messages[0][1]
    assert publisher.recorded_messages[0][3] is False
    assert publisher.recorded_pins == [2]


def test_force_reconcile_accepts_already_correct_telegram_text(monkeypatch) -> None:
    publisher = _BoardHarness()
    methods: list[str] = []

    monkeypatch.setattr(
        "app.telegram_live_board_reconcile._current_live_board_message_id",
        lambda manager: 2,
    )

    def fake_bot_call(token, method, payload):
        del token, payload
        methods.append(method)
        if method == "editMessageText":
            raise TelegramPublishError(
                "telegram_http_400",
                "Bad Request: message is not modified",
            )
        return {"message_id": 2}

    monkeypatch.setattr("app.telegram_live_board_reconcile._bot_api_call", fake_bot_call)

    assert force_reconcile_live_board(publisher) is True
    assert methods == ["editMessageText", "unpinChatMessage", "pinChatMessage"]
    assert publisher.recorded_messages[0][0] == 2
    assert publisher.recorded_pins == [2]
