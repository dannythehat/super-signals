"""Read-only Telegram dialog discovery and bounded Provider Lab sampling."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from telethon import TelegramClient
from telethon.sessions import StringSession

from app.telegram_gateway import TelegramSessionInvalidError


class TelegramSourceGatewayError(RuntimeError):
    """Raised when Telegram source discovery cannot complete safely."""


@dataclass(frozen=True, slots=True)
class TelegramSelectableDialog:
    chat_id: int
    title: str
    kind: Literal["group", "channel"]


@dataclass(frozen=True, slots=True)
class TelegramResearchMessage:
    telegram_message_id: int
    raw_text: str
    posted_at: datetime
    edited_at: datetime | None


@dataclass(frozen=True, slots=True)
class TelegramResearchDialog:
    chat_id: int
    title: str
    kind: Literal["group", "channel"]
    messages: tuple[TelegramResearchMessage, ...]


_RESEARCH_TITLE_HINT = re.compile(
    r"\b(?:xau(?:usd)?|gold|forex|fx|trade(?:r|rs|s|ing)?|signal(?:s)?|scalp(?:er|ing)?)\b",
    re.IGNORECASE,
)


class TelegramSourceGateway(Protocol):
    async def list_selectable_dialogs(
        self, session_string: str
    ) -> list[TelegramSelectableDialog]: ...

    async def scan_joined_research_dialogs(
        self,
        session_string: str,
        *,
        history_limit: int = 30,
    ) -> list[TelegramResearchDialog]: ...


class TelethonTelegramSourceGateway:
    """Read Telegram sources already visible to the connected private reader.

    Provider Lab never joins, leaves, archives or mutes Telegram chats. It only samples
    a bounded number of recent text messages from already joined likely trading dialogs.
    """

    def __init__(self, api_id: int, api_hash: str) -> None:
        self._api_id = api_id
        self._api_hash = api_hash

    def _client(self, session_string: str) -> TelegramClient:
        client = TelegramClient(
            StringSession(session_string),
            self._api_id,
            self._api_hash,
            device_model="Super Signals Server",
            app_version="0.6",
            system_lang_code="en",
            lang_code="en",
        )
        client.session.save_entities = False
        return client

    async def list_selectable_dialogs(
        self, session_string: str
    ) -> list[TelegramSelectableDialog]:
        client = self._client(session_string)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise TelegramSessionInvalidError(
                    "The stored Telegram session is no longer authorised."
                )

            selectable: list[TelegramSelectableDialog] = []
            async for dialog in client.iter_dialogs():
                option = self._to_selectable_dialog(dialog)
                if option is not None:
                    selectable.append(option)

            selectable.sort(key=lambda item: (item.title.casefold(), item.chat_id))
            return selectable
        except TelegramSessionInvalidError:
            raise
        except Exception as exc:
            raise TelegramSourceGatewayError(
                "Telegram groups and channels could not be listed."
            ) from exc
        finally:
            if client.is_connected():
                await client.disconnect()

    async def scan_joined_research_dialogs(
        self,
        session_string: str,
        *,
        history_limit: int = 30,
    ) -> list[TelegramResearchDialog]:
        safe_limit = min(max(int(history_limit), 5), 100)
        client = self._client(session_string)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise TelegramSessionInvalidError(
                    "The stored Telegram session is no longer authorised."
                )

            candidates: list[TelegramResearchDialog] = []
            async for dialog in client.iter_dialogs():
                option = self._to_selectable_dialog(dialog)
                if option is None or _RESEARCH_TITLE_HINT.search(option.title) is None:
                    continue

                samples: list[TelegramResearchMessage] = []
                entity = getattr(dialog, "entity", None)
                if entity is None:
                    continue
                async for message in client.iter_messages(entity, limit=safe_limit):
                    raw_text = str(getattr(message, "raw_text", "") or "").strip()
                    if not raw_text:
                        continue
                    message_id = getattr(message, "id", None)
                    if message_id is None:
                        continue
                    posted_at = self._utc_datetime(getattr(message, "date", None))
                    edited_at = self._utc_datetime(getattr(message, "edit_date", None), optional=True)
                    samples.append(
                        TelegramResearchMessage(
                            telegram_message_id=int(message_id),
                            raw_text=raw_text,
                            posted_at=posted_at,
                            edited_at=edited_at,
                        )
                    )

                candidates.append(
                    TelegramResearchDialog(
                        chat_id=option.chat_id,
                        title=option.title,
                        kind=option.kind,
                        messages=tuple(samples),
                    )
                )

            candidates.sort(key=lambda item: (item.title.casefold(), item.chat_id))
            return candidates
        except TelegramSessionInvalidError:
            raise
        except Exception as exc:
            raise TelegramSourceGatewayError(
                "Telegram Provider Lab could not sample joined research channels."
            ) from exc
        finally:
            if client.is_connected():
                await client.disconnect()

    @staticmethod
    def _utc_datetime(value: object, *, optional: bool = False) -> datetime | None:
        if not isinstance(value, datetime):
            return None if optional else datetime.now(UTC)
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def _to_selectable_dialog(dialog: object) -> TelegramSelectableDialog | None:
        if bool(getattr(dialog, "is_user", False)):
            return None

        is_group = bool(getattr(dialog, "is_group", False))
        is_channel = bool(getattr(dialog, "is_channel", False))
        if not is_group and not is_channel:
            return None

        raw_id = getattr(dialog, "id", None)
        raw_title = getattr(dialog, "title", None)
        if raw_id is None or not isinstance(raw_title, str):
            return None
        title = raw_title.strip()
        if not title:
            return None

        kind: Literal["group", "channel"] = "group" if is_group else "channel"
        return TelegramSelectableDialog(chat_id=int(raw_id), title=title, kind=kind)
