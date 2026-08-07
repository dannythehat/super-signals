"""Read-only Telegram dialog discovery for explicit signal-source selection."""

from __future__ import annotations

from dataclasses import dataclass
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


class TelegramSourceGateway(Protocol):
    async def list_selectable_dialogs(
        self, session_string: str
    ) -> list[TelegramSelectableDialog]: ...


class TelethonTelegramSourceGateway:
    """List groups/channels only and never inspect or persist message content."""

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

    @staticmethod
    def _to_selectable_dialog(dialog: object) -> TelegramSelectableDialog | None:
        # Explicitly reject user dialogs first. This includes private one-to-one chats and bots.
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

        # Supergroups can report as both group and channel. Present them as groups.
        kind: Literal["group", "channel"] = "group" if is_group else "channel"
        return TelegramSelectableDialog(chat_id=int(raw_id), title=title, kind=kind)
