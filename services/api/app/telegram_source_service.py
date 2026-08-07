"""Explicit Telegram group/channel selection without starting monitoring."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AuditEvent, Source, TelegramAccount
from app.telegram_crypto import TelegramSessionCipher
from app.telegram_gateway import TelegramSessionInvalidError
from app.telegram_source_gateway import (
    TelegramSelectableDialog,
    TelegramSourceGateway,
    TelethonTelegramSourceGateway,
)


class TelegramSourceConfigurationError(RuntimeError):
    """Raised when Telegram source discovery is not configured."""


class TelegramSourceNotFoundError(LookupError):
    """Raised when an account or source cannot be found for the actor."""


@dataclass(frozen=True, slots=True)
class TelegramSelectableSourceView:
    chat_id: int
    title: str
    kind: str
    selected: bool
    source_id: UUID | None
    status: str | None


class TelegramSourceService:
    def __init__(self, gateway: TelegramSourceGateway, cipher: TelegramSessionCipher) -> None:
        self._gateway = gateway
        self._cipher = cipher

    async def discover_sources(
        self,
        session: Session,
        *,
        actor: dict[str, Any],
        account_id: UUID,
    ) -> list[TelegramSelectableSourceView]:
        account = self._connected_account_for_actor(session, actor, account_id)
        session_string = self._cipher.decrypt(account.session_ciphertext)
        dialogs = await self._gateway.list_selectable_dialogs(session_string)

        selected_rows = session.scalars(
            select(Source).where(Source.telegram_account_id == account.id)
        ).all()
        by_chat_id = {source.chat_id: source for source in selected_rows}

        views: list[TelegramSelectableSourceView] = []
        for dialog in dialogs:
            if dialog.kind not in {"group", "channel"}:
                continue
            source = by_chat_id.get(dialog.chat_id)
            is_selected = source is not None and source.status != "revoked"
            views.append(
                TelegramSelectableSourceView(
                    chat_id=dialog.chat_id,
                    title=dialog.title,
                    kind=dialog.kind,
                    selected=is_selected,
                    source_id=source.id if is_selected else None,
                    status=source.status if is_selected else None,
                )
            )
        return views

    async def select_source(
        self,
        session: Session,
        *,
        actor: dict[str, Any],
        account_id: UUID,
        chat_id: int,
    ) -> TelegramSelectableSourceView:
        account = self._connected_account_for_actor(session, actor, account_id)
        session_string = self._cipher.decrypt(account.session_ciphertext)
        dialogs = await self._gateway.list_selectable_dialogs(session_string)
        dialog = next(
            (
                candidate
                for candidate in dialogs
                if candidate.chat_id == chat_id and candidate.kind in {"group", "channel"}
            ),
            None,
        )
        if dialog is None:
            raise TelegramSourceNotFoundError(
                "That Telegram chat is not available as a selectable group or channel."
            )

        source = session.scalar(
            select(Source).where(
                Source.telegram_account_id == account.id,
                Source.chat_id == dialog.chat_id,
            )
        )
        if source is None:
            source = Source(
                telegram_account_id=account.id,
                chat_id=dialog.chat_id,
                chat_title=dialog.title[:255],
                source_alias=self._default_alias(dialog),
                status="paused",
                redistribution_permission_confirmed=False,
                created_by_user_id=actor["id"],
            )
            session.add(source)
        else:
            source.chat_title = dialog.title[:255]
            source.status = "paused"

        session.flush()
        self._audit(
            session,
            actor_id=actor["id"],
            event_type="telegram.source_selected",
            source_id=source.id,
            payload={
                "telegram_account_id": str(account.id),
                "chat_id": dialog.chat_id,
                "kind": dialog.kind,
                "status": "paused",
                "monitoring_started": False,
            },
        )
        session.commit()
        session.refresh(source)
        return TelegramSelectableSourceView(
            chat_id=dialog.chat_id,
            title=dialog.title,
            kind=dialog.kind,
            selected=True,
            source_id=source.id,
            status=source.status,
        )

    def unselect_source(
        self,
        session: Session,
        *,
        actor: dict[str, Any],
        account_id: UUID,
        source_id: UUID,
    ) -> dict[str, Any]:
        account = self._account_for_actor(session, actor, account_id)
        source = session.scalar(
            select(Source).where(
                Source.id == source_id,
                Source.telegram_account_id == account.id,
            )
        )
        if source is None:
            raise TelegramSourceNotFoundError("Selected Telegram source was not found.")

        source.status = "revoked"
        self._audit(
            session,
            actor_id=actor["id"],
            event_type="telegram.source_unselected",
            source_id=source.id,
            payload={
                "telegram_account_id": str(account.id),
                "chat_id": source.chat_id,
                "monitoring_started": False,
            },
        )
        session.commit()
        return {
            "removed": True,
            "source_id": source.id,
            "monitoring_started": False,
        }

    @staticmethod
    def _default_alias(dialog: TelegramSelectableDialog) -> str:
        alias = dialog.title.strip()[:120]
        return alias or f"Telegram {dialog.kind}"

    @classmethod
    def _connected_account_for_actor(
        cls,
        session: Session,
        actor: dict[str, Any],
        account_id: UUID,
    ) -> TelegramAccount:
        account = cls._account_for_actor(session, actor, account_id)
        if account.status != "connected":
            raise TelegramSessionInvalidError(
                "Connect and verify the Telegram reader before selecting sources."
            )
        return account

    @staticmethod
    def _account_for_actor(
        session: Session,
        actor: dict[str, Any],
        account_id: UUID,
    ) -> TelegramAccount:
        statement = select(TelegramAccount).where(TelegramAccount.id == account_id)
        if actor["role"] != "owner":
            statement = statement.where(TelegramAccount.owner_user_id == actor["id"])
        account = session.scalar(statement)
        if account is None:
            raise TelegramSourceNotFoundError("Telegram account was not found.")
        return account

    @staticmethod
    def _audit(
        session: Session,
        *,
        actor_id: UUID,
        event_type: str,
        source_id: UUID,
        payload: dict[str, Any],
    ) -> None:
        session.add(
            AuditEvent(
                actor_user_id=actor_id,
                event_type=event_type,
                entity_type="source",
                entity_id=source_id,
                payload=payload,
            )
        )


@lru_cache
def get_telegram_source_service() -> TelegramSourceService:
    settings = get_settings()
    if settings.telegram_api_id is None or settings.telegram_api_hash is None:
        raise TelegramSourceConfigurationError(
            "Telegram API credentials are not configured on this server."
        )
    return TelegramSourceService(
        gateway=TelethonTelegramSourceGateway(
            settings.telegram_api_id,
            settings.telegram_api_hash,
        ),
        cipher=TelegramSessionCipher(settings.telegram_session_keys),
    )
