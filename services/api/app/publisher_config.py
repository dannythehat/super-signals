"""Environment settings for the separate publish-only Telegram bot."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class PublisherSettings:
    enabled: bool
    bot_token: str | None
    destination_chat_id: int | None
    poll_seconds: int


def _optional_chat_id(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        return int(value.strip())
    except ValueError as exc:
        raise RuntimeError("SUPER_SIGNALS_TELEGRAM_PUBLISH_CHAT_ID must be an integer") from exc


@lru_cache
def get_publisher_settings() -> PublisherSettings:
    enabled = _parse_bool(os.getenv("SUPER_SIGNALS_TELEGRAM_PUBLISHER_ENABLED", "false"))
    token = os.getenv("SUPER_SIGNALS_TELEGRAM_PUBLISH_BOT_TOKEN")
    token = token.strip() if token and token.strip() else None
    destination_chat_id = _optional_chat_id(os.getenv("SUPER_SIGNALS_TELEGRAM_PUBLISH_CHAT_ID"))
    try:
        poll_seconds = int(os.getenv("SUPER_SIGNALS_TELEGRAM_PUBLISH_POLL_SECONDS", "3"))
    except ValueError as exc:
        raise RuntimeError("SUPER_SIGNALS_TELEGRAM_PUBLISH_POLL_SECONDS must be a positive integer") from exc
    if poll_seconds <= 0:
        raise RuntimeError("SUPER_SIGNALS_TELEGRAM_PUBLISH_POLL_SECONDS must be a positive integer")
    if enabled and (token is None or destination_chat_id is None):
        raise RuntimeError(
            "SUPER_SIGNALS_TELEGRAM_PUBLISH_BOT_TOKEN and SUPER_SIGNALS_TELEGRAM_PUBLISH_CHAT_ID must be configured when the publisher is enabled"
        )
    return PublisherSettings(
        enabled=enabled,
        bot_token=token,
        destination_chat_id=destination_chat_id,
        poll_seconds=poll_seconds,
    )
