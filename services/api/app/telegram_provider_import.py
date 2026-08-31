"""Provider Lab import service for public Telegram Gold/XAUUSD research channels."""

from __future__ import annotations

from functools import lru_cache
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AuditEvent, Source, TelegramAccount
from app.telegram_crypto import TelegramSessionCipher
from app.telegram_gateway import TelegramSessionInvalidError
from app.telegram_source_gateway import TelethonTelegramSourceGateway
from app.telegram_source_service import (
    TelegramSelectableSourceView,
    TelegramSourceConfigurationError,
    TelegramSourceNotFoundError,
)


class PublicProviderImportService:
    """Join a public Telegram channel and register it as isolated research."""

    def __init__(
        self,
        gateway: TelethonTelegramSourceGateway,
        cipher: TelegramSessionCipher,
    ) -> None:
        self._gateway = gateway
        self._cipher = cipher

    async def import_public_source(
        self,
        session: Session,
        *,
        actor: dict[str, Any],
        account_id: UUID,
        identifier: str,
    ) -> TelegramSelectableSourceView:
        account = self._connected_account_for_actor(session, actor, account_id)
        session_string = self._cipher.decrypt(account.session_ciphertext)
        dialog = await self._gateway.join_public_source(session_string, identifier)

        source = session.scalar(
            select(Source)
            .where(Source.chat_id == dialog.chat_id, Source.status != "revoked")
            .order_by(Source.created_at.asc())
        )
        reused = source is not None

        if source is not None and source.status == "live":
            raise ValueError(
                "That channel already exists as LIVE. Provider Lab will not alter a live source."
            )

        if source is None:
            source = session.scalar(
                select(Source)
                .where(Source.chat_id == dialog.chat_id, Source.status == "revoked")
                .order_by(Source.created_at.asc())
            )

        if source is None:
            source = Source(
                telegram_account_id=account.id,
                chat_id=dialog.chat_id,
                chat_title=dialog.title[:255],
                source_alias=dialog.title.strip()[:120] or f"Telegram {dialog.kind}",
                status="shadow",
                redistribution_permission_confirmed=False,
                created_by_user_id=actor["id"],
            )
            session.add(source)
        else:
            source.chat_title = dialog.title[:255]
            if source.status == "revoked":
                source.telegram_account_id = account.id
                source.status = "shadow"
                reused = False

        try:
            session.flush()
        except IntegrityError as exc:
            session.rollback()
            source = session.scalar(
                select(Source)
                .where(Source.chat_id == dialog.chat_id, Source.status != "revoked")
                .order_by(Source.created_at.asc())
            )
            if source is None:
                raise exc
            if source.status == "live":
                raise ValueError(
                    "That channel already exists as LIVE. Provider Lab will not alter a live source."
                ) from exc
            reused = True

        self._ensure_reader_access(
            session,
            source_id=source.id,
            telegram_account_id=account.id,
            actor_id=actor["id"],
        )
        session.add(
            AuditEvent(
                actor_user_id=actor["id"],
                event_type="telegram.provider_lab_public_imported",
                entity_type="source",
                entity_id=source.id,
                payload={
                    "telegram_account_id": str(account.id),
                    "public_identifier": identifier.strip(),
                    "chat_id": dialog.chat_id,
                    "kind": dialog.kind,
                    "status": source.status,
                    "research_import": True,
                    "shared_source_reused": reused,
                    "monitoring_started": source.status == "shadow",
                    "live_trading_enabled": False,
                },
            )
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
            managed_by_this_reader=True,
        )

    @staticmethod
    def _ensure_reader_access(
        session: Session,
        *,
        source_id: UUID,
        telegram_account_id: UUID,
        actor_id: UUID,
    ) -> None:
        session.execute(
            text(
                """
                INSERT INTO source_reader_access (
                    source_id,
                    telegram_account_id,
                    created_by_user_id
                )
                VALUES (:source_id, :telegram_account_id, :actor_id)
                ON CONFLICT (source_id, telegram_account_id) DO NOTHING
                """
            ),
            {
                "source_id": source_id,
                "telegram_account_id": telegram_account_id,
                "actor_id": actor_id,
            },
        )

    @classmethod
    def _connected_account_for_actor(
        cls,
        session: Session,
        actor: dict[str, Any],
        account_id: UUID,
    ) -> TelegramAccount:
        account = session.scalar(
            select(TelegramAccount).where(
                TelegramAccount.id == account_id,
                TelegramAccount.owner_user_id == actor["id"],
            )
        )
        if account is None:
            raise TelegramSourceNotFoundError("Telegram account was not found.")
        if account.status != "connected":
            raise TelegramSessionInvalidError(
                "Connect and verify the Telegram reader before importing providers."
            )
        return account


@lru_cache
def get_public_provider_import_service() -> PublicProviderImportService:
    settings = get_settings()
    if settings.telegram_api_id is None or settings.telegram_api_hash is None:
        raise TelegramSourceConfigurationError(
            "Telegram API credentials are not configured on this server."
        )
    return PublicProviderImportService(
        gateway=TelethonTelegramSourceGateway(
            settings.telegram_api_id,
            settings.telegram_api_hash,
        ),
        cipher=TelegramSessionCipher(settings.telegram_session_keys),
    )
