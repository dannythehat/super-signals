from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.telegram_entity_recovery import read_messages_with_entity_recovery


class _Client:
    def __init__(self, dialogs, *, numeric_error: str | None = None) -> None:
        self._dialogs = dialogs
        self._numeric_error = numeric_error
        self.calls = []
        self.dialog_calls = 0

    async def get_messages(self, entity, *args, **kwargs):
        self.calls.append((entity, kwargs))
        if isinstance(entity, int) and self._numeric_error is not None:
            raise ValueError(self._numeric_error)
        return ["message"]

    async def iter_dialogs(self):
        self.dialog_calls += 1
        for item in self._dialogs:
            yield item

    async def get_input_entity(self, entity):
        return getattr(entity, "input_entity", entity)


@pytest.mark.asyncio
async def test_numeric_channel_recovers_input_entity_from_dialog() -> None:
    recovered = object()
    client = _Client(
        [SimpleNamespace(id=-1002176701424, input_entity=recovered)],
        numeric_error="cold entity cache",
    )

    result = await read_messages_with_entity_recovery(
        client,
        -1002176701424,
        limit=25,
    )
    assert result == ["message"]
    assert client.calls[0][0] == -1002176701424
    assert client.calls[1][0] is recovered
    assert client.calls[1][1]["limit"] == 25
    assert client.dialog_calls == 1


@pytest.mark.asyncio
async def test_non_numeric_entity_error_is_not_hidden() -> None:
    class _NamedClient(_Client):
        async def get_messages(self, entity, *args, **kwargs):
            raise ValueError("real error")

    client = _NamedClient([])
    with pytest.raises(ValueError, match="real error"):
        await read_messages_with_entity_recovery(client, "channel-name")


@pytest.mark.asyncio
async def test_removed_numeric_channel_isolated_without_repeat_dialog_scans() -> None:
    client = _Client(
        [SimpleNamespace(id=-100123, input_entity=object())],
        numeric_error="missing channel",
    )

    first = await read_messages_with_entity_recovery(client, -1002176701424)
    second = await read_messages_with_entity_recovery(client, -1002176701424)

    assert first == []
    assert second == []
    assert len(client.calls) == 1
    assert client.dialog_calls == 1


@pytest.mark.asyncio
async def test_telegram_internal_failure_pauses_history_without_dialog_hammering() -> None:
    client = _Client(
        [SimpleNamespace(id=-1002176701424, input_entity=object())],
        numeric_error="Request was unsuccessful 6 time(s)",
    )

    first = await read_messages_with_entity_recovery(
        client,
        -1002176701424,
        limit=50,
    )
    second = await read_messages_with_entity_recovery(
        client,
        -1002176701424,
        limit=50,
    )

    assert first == []
    assert second == []
    assert len(client.calls) == 1
    assert client.dialog_calls == 0


@pytest.mark.asyncio
async def test_fxtradingvision_public_fallback_pins_peer_and_ignores_old_posts() -> None:
    class PublicClient(_Client):
        async def get_input_entity(self, entity):
            assert entity == "fxtradingvision"
            return SimpleNamespace(channel_id=1651583302)

        async def get_messages(self, entity, *args, **kwargs):
            if isinstance(entity, int):
                return await super().get_messages(entity, *args, **kwargs)
            self.calls.append((entity, kwargs))
            return self.messages

    client = PublicClient([], numeric_error="cold entity cache")
    client.messages = [SimpleNamespace(id=1, date=datetime.now(timezone.utc) - timedelta(minutes=1))]
    assert await read_messages_with_entity_recovery(
        client, -1001651583302, limit=50, allow_verified_public_history=True,
    ) == []
    client.messages.append(SimpleNamespace(id=2, date=datetime.now(timezone.utc)))
    result = await read_messages_with_entity_recovery(
        client, -1001651583302, limit=50, allow_verified_public_history=True,
    )
    assert [message.id for message in result] == [2]
    assert client.dialog_calls == 1


@pytest.mark.asyncio
async def test_public_fallback_rejects_a_renamed_or_reassigned_handle() -> None:
    class WrongPeerClient(_Client):
        async def get_input_entity(self, entity):
            return SimpleNamespace(channel_id=1111111111)

    client = WrongPeerClient([], numeric_error="cold entity cache")
    result = await read_messages_with_entity_recovery(
        client, -1001651583302, limit=50, allow_verified_public_history=True,
    )
    assert result == []
    assert len(client.calls) == 1
