from types import SimpleNamespace

import pytest

from app.telegram_entity_recovery import get_messages_with_entity_recovery


class _Client:
    def __init__(self, dialogs):
        self._dialogs = dialogs

    async def iter_dialogs(self):
        for item in self._dialogs:
            yield item

    async def get_input_entity(self, entity):
        return getattr(entity, "input_entity", entity)


@pytest.mark.asyncio
async def test_numeric_channel_recovers_input_entity_from_dialog() -> None:
    recovered = object()
    client = _Client([SimpleNamespace(id=-1002176701424, input_entity=recovered)])
    calls = []

    async def original(_client, entity, *args, **kwargs):
        calls.append((entity, kwargs))
        if entity == -1002176701424:
            raise ValueError("cold entity cache")
        return ["message"]

    result = await get_messages_with_entity_recovery(
        client,
        original,
        -1002176701424,
        limit=25,
    )
    assert result == ["message"]
    assert calls[0][0] == -1002176701424
    assert calls[1][0] is recovered
    assert calls[1][1]["limit"] == 25


@pytest.mark.asyncio
async def test_non_numeric_entity_error_is_not_hidden() -> None:
    client = _Client([])

    async def original(_client, entity, *args, **kwargs):
        raise ValueError("real error")

    with pytest.raises(ValueError, match="real error"):
        await get_messages_with_entity_recovery(client, original, "channel-name")


@pytest.mark.asyncio
async def test_unknown_numeric_channel_still_fails_closed() -> None:
    client = _Client([SimpleNamespace(id=-100123, input_entity=object())])

    async def original(_client, entity, *args, **kwargs):
        raise ValueError("missing channel")

    with pytest.raises(ValueError, match="missing channel"):
        await get_messages_with_entity_recovery(client, original, -1002176701424)
