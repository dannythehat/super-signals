"""Read-only Telegram dialog discovery plus controlled public research-channel joins."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.parse import urlparse

from telethon import TelegramClient
from telethon.errors import (
    ChannelPrivateError,
    UserAlreadyParticipantError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.utils import get_peer_id

from app.telegram_gateway import TelegramSessionInvalidError


class TelegramSourceGatewayError(RuntimeError):
    """Raised when Telegram source discovery cannot complete safely."""


@dataclass(frozen=True, slots=True)
class TelegramSelectableDialog:
    chat_id: int
    title: str
    kind: Literal["group", "channel"]


def normalize_public_telegram_identifier(value: str) -> str:
    """Return a public Telegram username and reject private/invite-link forms."""

    raw = value.strip()
    if not raw:
        raise ValueError("Enter a public Telegram @username or t.me link.")

    if raw.startswith("@"):
        username = raw[1:]
    else:
        candidate = raw
        if "://" not in candidate and candidate.lower().startswith(("t.me/", "telegram.me/")):
            candidate = f"https://{candidate}"

        if "://" in candidate:
            parsed = urlparse(candidate)
            host = (parsed.hostname or "").lower()
            if host not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}:
                raise ValueError("Only public t.me Telegram links can be imported.")
            segments = [segment for segment in parsed.path.split("/") if segment]
            if segments and segments[0].lower() == "s":
                segments = segments[1:]
            if len(segments) != 1:
                raise ValueError("Use a public Telegram channel link, not a private invite link.")
            username = segments[0]
        else:
            username = candidate

    if username.startswith("+") or username.lower().startswith("joinchat"):
        raise ValueError("Private Telegram invite links are not allowed in Provider Lab.")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", username):
        raise ValueError("That does not look like a valid public Telegram username.")
    return username


class TelegramSourceGateway(Protocol):
    async def list_selectable_dialogs(
        self, session_string: str
    ) -> list[TelegramSelectableDialog]: ...

    async def join_public_source(
        self, session_string: str, identifier: str
    ) -> TelegramSelectableDialog: ...


class TelethonTelegramSourceGateway:
    """List selected dialogs and explicitly join public research channels only."""

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

    async def join_public_source(
        self, session_string: str, identifier: str
    ) -> TelegramSelectableDialog:
        username = normalize_public_telegram_identifier(identifier)
        client = self._client(session_string)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise TelegramSessionInvalidError(
                    "The stored Telegram session is no longer authorised."
                )

            entity = await client.get_entity(username)
            option = self._entity_to_selectable_dialog(entity)
            if option is None:
                raise ValueError("That Telegram username is not a public group or channel.")

            try:
                await client(JoinChannelRequest(entity))
            except UserAlreadyParticipantError:
                pass

            # Resolve again after the join so the listener receives the canonical marked chat id.
            entity = await client.get_entity(username)
            option = self._entity_to_selectable_dialog(entity)
            if option is None:
                raise ValueError("That Telegram username is not a public group or channel.")
            return option
        except TelegramSessionInvalidError:
            raise
        except (UsernameInvalidError, UsernameNotOccupiedError, ChannelPrivateError) as exc:
            raise ValueError("That public Telegram channel could not be found or joined.") from exc
        except ValueError:
            raise
        except Exception as exc:
            raise TelegramSourceGatewayError(
                "Telegram could not join the public research channel."
            ) from exc
        finally:
            if client.is_connected():
                await client.disconnect()

    @staticmethod
    def _entity_to_selectable_dialog(entity: object) -> TelegramSelectableDialog | None:
        title = str(getattr(entity, "title", "") or "").strip()
        if not title:
            return None
        is_group = bool(getattr(entity, "megagroup", False))
        is_channel = bool(getattr(entity, "broadcast", False))
        if not is_group and not is_channel:
            return None
        return TelegramSelectableDialog(
            chat_id=int(get_peer_id(entity)),
            title=title,
            kind="group" if is_group else "channel",
        )

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
