"""Resilient Telegram history reads when Telethon's in-memory entity cache is cold.

Super Signals intentionally does not persist Telethon entity caches in the encrypted
StringSession. After a restart, a numeric channel id can therefore receive live push
updates but `get_messages(chat_id)` may fail because Telethon no longer has that
channel's access hash. Recovery/catch-up must be able to rebuild the entity from the
reader account's dialog list instead of silently losing gap protection for that source.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from telethon import TelegramClient

logger = logging.getLogger(__name__)

_installed = False
_original_get_messages: Callable[..., Awaitable[Any]] | None = None
_COOLDOWN_SECONDS = 300.0
_TELETHON_CHANNEL_MARK = 1_000_000_000_000


def _canonical_channel_id(value: Any) -> int | None:
    """Return the raw Telegram channel id regardless of Telethon id representation.

    Telegram/Telethon can represent one channel as either the raw PeerChannel id
    (for example ``2176701424``) or the marked peer id (``-1002176701424``).  A
    strict integer comparison therefore makes a readable channel look missing after
    a cold restart.  Normalize both forms to the same raw positive id.
    """
    try:
        parsed = abs(int(value))
    except (TypeError, ValueError):
        return None
    if parsed >= _TELETHON_CHANNEL_MARK:
        marked = str(parsed)
        if marked.startswith("100"):
            return parsed - _TELETHON_CHANNEL_MARK
    return parsed


async def get_messages_with_entity_recovery(
    client: Any,
    original: Callable[..., Awaitable[Any]],
    entity: Any,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Retry one failed numeric-channel history read using a dialog input entity."""
    try:
        return await original(client, entity, *args, **kwargs)
    except ValueError as exc:
        if not isinstance(entity, int):
            raise

        target = int(entity)
        canonical_target = _canonical_channel_id(target)
        missing_until = getattr(client, "_super_signals_missing_entity_until", None)
        if not isinstance(missing_until, dict):
            missing_until = {}
            setattr(client, "_super_signals_missing_entity_until", missing_until)
        if float(missing_until.get(target, 0.0)) > time.monotonic():
            raise exc

        async for dialog in client.iter_dialogs():
            dialog_id = _canonical_channel_id(getattr(dialog, "id", None))
            if dialog_id is None or dialog_id != canonical_target:
                continue
            input_entity = getattr(dialog, "input_entity", None)
            if input_entity is None:
                dialog_entity = getattr(dialog, "entity", None)
                if dialog_entity is not None:
                    input_entity = await client.get_input_entity(dialog_entity)
            if input_entity is None:
                break

            missing_until.pop(target, None)
            logger.info("Recovered Telegram input entity for history reconciliation")
            return await original(client, input_entity, *args, **kwargs)

        missing_until[target] = time.monotonic() + _COOLDOWN_SECONDS
        raise exc


def install_telegram_entity_recovery() -> None:
    """Install the narrow get_messages fallback once for all listener clients."""
    global _installed, _original_get_messages
    if _installed:
        return

    _original_get_messages = TelegramClient.get_messages

    async def wrapped(self: Any, entity: Any, *args: Any, **kwargs: Any) -> Any:
        assert _original_get_messages is not None
        return await get_messages_with_entity_recovery(
            self,
            _original_get_messages,
            entity,
            *args,
            **kwargs,
        )

    TelegramClient.get_messages = wrapped  # type: ignore[method-assign]
    _installed = True


__all__ = [
    "_canonical_channel_id",
    "get_messages_with_entity_recovery",
    "install_telegram_entity_recovery",
]
