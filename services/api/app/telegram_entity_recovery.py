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
from datetime import datetime, timezone
from typing import Any

from telethon.errors import RPCError

logger = logging.getLogger(__name__)

_COOLDOWN_SECONDS = 300.0
_TRANSIENT_HISTORY_COOLDOWN_SECONDS = 30.0
_TELETHON_CHANNEL_MARK = 1_000_000_000_000
_PUBLIC_HISTORY_SOURCES = {-1001651583302: "fxtradingvision"}

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


def _missing_entity_cache(client: Any) -> dict[int, float]:
    raw = getattr(client, "_super_signals_missing_entity_until", None)
    if isinstance(raw, dict):
        return raw
    cache: dict[int, float] = {}
    setattr(client, "_super_signals_missing_entity_until", cache)
    return cache


def _known_missing_entity(client: Any, entity: Any) -> bool:
    if not isinstance(entity, int):
        return False
    raw = _missing_entity_cache(client).get(int(entity), 0.0)
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
    allow_verified_public_history: bool = False,
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
    if _history_outage_active(client) or _known_missing_entity(client, entity):
        return []

    public_name = None
    if allow_verified_public_history and isinstance(entity, int):
        public_name = _PUBLIC_HISTORY_SOURCES.get(entity)
    public_history = getattr(client, "_super_signals_public_history", None)
    if public_name and public_history is not None:
        return await _read_forward_public_history(client, *public_history, args, kwargs)

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
        missing_until = _missing_entity_cache(client)

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
            logger.info("Recovered Telegram input entity for history reconciliation chat_id=%d", target)
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

        if public_name:
            try:
                peer = await client.get_input_entity(public_name)
            except (ValueError, RPCError) as exc:
                logger.warning("Telegram public source lookup failed chat_id=%d error=%s", target, type(exc).__name__)
            else:
                if getattr(peer, "channel_id", None) == canonical_target:
                    # A reader outside the channel has no push subscription. Use the
                    # existing bounded history sweep only for posts after this cutover.
                    public_history = (peer, datetime.now(timezone.utc))
                    client._super_signals_public_history = public_history
                    messages = await client.get_messages(peer, *args, **kwargs)
                    logger.warning(
                        "Telegram push reader lacks source dialog; verified public recovery chat_id=%d "
                        "username=%s newest_message_id=%s", target, public_name,
                        getattr(next(iter(messages), None), "id", None),
                    )
                    return _forward_messages(messages, public_history[1])
                logger.warning("Telegram public source peer mismatch chat_id=%d username=%s", target, public_name)

        missing_until[target] = time.monotonic() + _COOLDOWN_SECONDS
        logger.warning("Telegram source absent from reader dialogs chat_id=%d retry_in_seconds=%.0f",
                       target, _COOLDOWN_SECONDS)
        return []


async def _read_forward_public_history(client, peer, cutover, args, kwargs):
    messages = await client.get_messages(peer, *args, **kwargs)
    return _forward_messages(messages, cutover)


def _forward_messages(messages, cutover):
    return [
        message for message in messages
        if getattr(message, "date", None) is not None and message.date > cutover
    ]


__all__ = ["_canonical_channel_id", "read_messages_with_entity_recovery"]
