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
_TRANSIENT_HISTORY_COOLDOWN_SECONDS = 30.0
_TELETHON_CHANNEL_MARK = 1_000_000_000_000

# A cold Telethon StringSession can legitimately know a numeric channel id without
# knowing the access hash needed for a history read. Only those entity-resolution
# failures should trigger the expensive dialog scan. Telegram RPC outages can also end
# as ValueError("Request was unsuccessful N time(s)"); treating those as a missing
# entity causes every source recovery task to hammer GetDialogs while Telegram is down.
_ENTITY_RESOLUTION_ERROR_MARKERS = (
    "cannot find any entity corresponding",
    "could not find the input entity",
    "cold entity cache",
    "missing channel",
)
_TRANSIENT_HISTORY_ERROR_MARKERS = (
    "request was unsuccessful",
    "telegram is having internal issues",
)


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


def _matches_error(exc: BaseException, markers: tuple[str, ...]) -> bool:
    message = str(exc).casefold()
    return any(marker in message for marker in markers)


def _history_outage_active(client: Any) -> bool:
    raw = getattr(client, "_super_signals_history_unavailable_until", 0.0)
    try:
        return float(raw) > time.monotonic()
    except (TypeError, ValueError):
        return False


def _defer_history_after_transient_error(client: Any) -> None:
    setattr(
        client,
        "_super_signals_history_unavailable_until",
        time.monotonic() + _TRANSIENT_HISTORY_COOLDOWN_SECONDS,
    )


async def read_messages_with_entity_recovery(
    client: Any,
    entity: Any,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Read Telegram history, resolving one cold numeric channel through dialogs.

    A short client-wide cooldown is used when Telegram itself is returning transient
    internal-history failures. Live Telegram push delivery remains registered throughout;
    only bounded history catch-up pauses, preventing recovery sweeps from amplifying an
    upstream outage with repeated GetHistory/GetDialogs calls.

    Numeric-channel entity-resolution failures still get one dialog-based recovery. If
    the reader is no longer a member of that source, historical catch-up is quietly
    deferred for that source instead of repeatedly throwing errors. Live push listening
    for every other selected source remains unaffected.
    """
    if _history_outage_active(client):
        return []

    try:
        return await client.get_messages(entity, *args, **kwargs)
    except ValueError as exc:
        if _matches_error(exc, _TRANSIENT_HISTORY_ERROR_MARKERS):
            _defer_history_after_transient_error(client)
            logger.warning(
                "Telegram history temporarily unavailable; deferring recovery for %.0fs",
                _TRANSIENT_HISTORY_COOLDOWN_SECONDS,
            )
            return []

        if not isinstance(entity, int) or not _matches_error(
            exc,
            _ENTITY_RESOLUTION_ERROR_MARKERS,
        ):
            raise

        target = int(entity)
        canonical_target = _canonical_channel_id(target)
        missing_until = getattr(client, "_super_signals_missing_entity_until", None)
        if not isinstance(missing_until, dict):
            missing_until = {}
            setattr(client, "_super_signals_missing_entity_until", missing_until)
        if float(missing_until.get(target, 0.0)) > time.monotonic():
            # The previous recovery attempt already proved this reader cannot resolve
            # this source right now. Do not repeat the same history/dialog scan every
            # 15 seconds; live Telegram push delivery remains registered throughout.
            return []

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
            try:
                return await client.get_messages(input_entity, *args, **kwargs)
            except ValueError as retry_exc:
                if _matches_error(retry_exc, _TRANSIENT_HISTORY_ERROR_MARKERS):
                    _defer_history_after_transient_error(client)
                    logger.warning(
                        "Telegram history temporarily unavailable after entity recovery; "
                        "deferring recovery for %.0fs",
                        _TRANSIENT_HISTORY_COOLDOWN_SECONDS,
                    )
                    return []
                raise

        missing_until[target] = time.monotonic() + _COOLDOWN_SECONDS
        logger.info(
            "Telegram source is no longer present in this reader; deferring history recovery for %.0fs",
            _COOLDOWN_SECONDS,
        )
        return []


__all__ = ["_canonical_channel_id", "read_messages_with_entity_recovery"]
