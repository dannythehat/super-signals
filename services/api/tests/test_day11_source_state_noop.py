"""Small Day 11 unit checks that do not require a PostgreSQL test database."""

from __future__ import annotations

from uuid import uuid4

from app.models import Source
from app.telegram_source_service import TelegramSourceService


class UnusedGateway:
    async def list_selectable_dialogs(self, session_string: str):
        del session_string
        return []


class UnusedCipher:
    pass


class RecordingSession:
    def __init__(self, source: Source) -> None:
        self.source = source
        self.added: list[object] = []
        self.commit_count = 0

    def get(self, model, entity_id):
        del model
        return self.source if entity_id == self.source.id else None

    def add(self, value: object) -> None:
        self.added.append(value)

    def commit(self) -> None:
        self.commit_count += 1


def test_requesting_current_source_state_is_a_noop() -> None:
    source = Source(
        id=uuid4(),
        telegram_account_id=uuid4(),
        chat_id=-100111,
        chat_title="Gold Signals",
        source_alias="Gold Signals",
        status="paused",
        redistribution_permission_confirmed=False,
    )
    session = RecordingSession(source)
    service = TelegramSourceService(UnusedGateway(), UnusedCipher())
    actor = {
        "id": uuid4(),
        "display_name": "Danny",
        "email": "owner@example.com",
        "role": "owner",
    }

    result = service.change_source_status(
        session,
        actor=actor,
        source_id=source.id,
        new_status="paused",
    )

    assert result.previous_status == "paused"
    assert result.status == "paused"
    assert source.status == "paused"
    assert session.added == []
    assert session.commit_count == 0
