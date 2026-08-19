"""Explicit Telegram history reads with cold-session entity recovery.

Super Signals intentionally does not persist Telethon entity caches in the encrypted
StringSession. After a restart, a numeric channel id can receive live push updates while
a history read still lacks the channel access hash. Canonical recovery therefore retries
one failed numeric-channel history read using the reader account's dialog input entity.

This module performs no global monkey-patching. Callers opt into the recovery helper at
the exact history-read boundary.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_COOLDOWN_SECONDS = 300.0
_TELETHON_CHANNEL_MARK = 1_000_000_000_000


def _canonical_channel_id(value: Any) -> int | None:
    """Return the raw Telegram channel id regardless of Telethon id representation."""
    try:
        parsed = abs(int(value))
    except (TypeError, ValueError):
        return None
    if parsed >= _TELETHON_CHANNEL_MARK:
        marked = str(parsed)
        if marked.startswith("100"):
            return parsed - _TELETHON_CHANNEL_MARK
    return parsed


async def read_messages_with_entity_recovery(
    client: Any,
    entity: Any,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Read Telegram history, resolving one cold numeric channel through dialogs.

    Only a ValueError for an integer channel id triggers recovery. Other failures keep
    their normal Telethon semantics. A missing entity is cached briefly on the client so
    repeated reconciliation sweeps do not hammer the dialog list.
    """
    try:
        return await client.get_messages(entity, *args, **kwargs)
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
            return await client.get_messages(input_entity, *args, **kwargs)

        missing_until[target] = time.monotonic() + _COOLDOWN_SECONDS
        raise exc


__all__ = ["_canonical_channel_id", "read_messages_with_entity_recovery"]
