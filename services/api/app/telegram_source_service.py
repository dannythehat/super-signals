"""Shared Telegram group/channel selection without starting monitoring."""

from __future__ import annotations

from dataclasses import dataclass
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
    managed_by_this_reader: bool


@dataclass(frozen=True, slots=True)
class SharedTelegramSourceView:
    source_id: UUID
    chat_id: int
    title: str
    status: str


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

        shared_rows = session.scalars(
            select(Source)
            .where(Source.status != "revoked")
            .order_by(Source.created_at.asc())
        ).all()
        shared_by_chat_id: dict[int, Source] = {}
        for source in shared_rows:
            shared_by_chat_id.setdefault(source.chat_id, source)

        reader_source_ids = self._reader_source_ids(session, account.id)
        views: list[TelegramSelectableSourceView] = []
        for dialog in dialogs:
            if dialog.kind not in {"group", "channel"}:
                continue
            shared_source = shared_by_chat_id.get(dialog.chat_id)
            is_selected = shared_source is not None
            views.append(
                TelegramSelectableSourceView(
                    chat_id=dialog.chat_id,
                    title=dialog.title,
                    kind=dialog.kind,
                    selected=is_selected,
                    source_id=shared_source.id if shared_source is not None else None,
                    status=shared_source.status if shared_source is not None else None,
                    managed_by_this_reader=(
                        shared_source.id in reader_source_ids if shared_source is not None else True
                    ),
                )
            )
        return views

    def list_shared_sources(self, session: Session) -> list[SharedTelegramSourceView]:
        rows = session.scalars(
            select(Source)
            .where(Source.status != "revoked")
            .order_by(Source.created_at.asc())
        ).all()
        return [
            SharedTelegramSourceView(
                source_id=source.id,
                chat_id=source.chat_id,
                title=source.chat_title or source.source_alias,
                status=source.status,
            )
            for source in rows
        ]

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
            select(Source)
            .where(
                Source.chat_id == dialog.chat_id,
                Source.status != "revoked",
            )
            .order_by(Source.created_at.asc())
        )
        shared_source_reused = source is not None

        if source is None:
            source = session.scalar(
                select(Source)
                .where(
                    Source.chat_id == dialog.chat_id,
                    Source.status == "revoked",
                )
                .order_by(Source.created_at.asc())
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
            if source.status == "revoked":
                source.telegram_account_id = account.id
                source.status = "paused"

        try:
            session.flush()
        except IntegrityError as exc:
            # The database enforces one active logical source per Telegram chat. If
            # two admins select the same group concurrently, keep the first source.
            session.rollback()
            source = session.scalar(
                select(Source)
                .where(
                    Source.chat_id == dialog.chat_id,
                    Source.status != "revoked",
                )
                .order_by(Source.created_at.asc())
            )
            if source is None:
                raise exc
            shared_source_reused = True

        self._ensure_reader_access(
            session,
            source_id=source.id,
            telegram_account_id=account.id,
            actor_id=actor["id"],
        )
        self._audit(
            session,
            actor_id=actor["id"],
            event_type="telegram.source_selected",
            source_id=source.id,
            payload={
                "telegram_account_id": str(account.id),
                "chat_id": dialog.chat_id,
                "kind": dialog.kind,
                "status": source.status,
                "monitoring_started": False,
                "shared_source_reused": shared_source_reused,
                "reader_access_added": True,
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
            managed_by_this_reader=True,
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
        source = session.get(Source, source_id)
        if source is None or source.status == "revoked":
            raise TelegramSourceNotFoundError("Selected Telegram source was not found.")
        if source_id not in self._reader_source_ids(session, account.id):
            raise TelegramSourceNotFoundError(
                "This reader does not have an access link to that Telegram source."
            )

        session.execute(
            text(
                """
                DELETE FROM source_reader_access
                WHERE source_id = :source_id
                  AND telegram_account_id = :telegram_account_id
                """
            ),
            {"source_id": source.id, "telegram_account_id": account.id},
        )
        remaining_reader_ids = self._source_reader_ids(session, source.id)
        source_revoked = not remaining_reader_ids
        if source_revoked:
            source.status = "revoked"
        elif source.telegram_account_id == account.id:
            # Keep the legacy/preferred reader pointer valid while the new access
            # table records every authorised fallback reader.
            source.telegram_account_id = remaining_reader_ids[0]

        self._audit(
            session,
            actor_id=actor["id"],
            event_type="telegram.source_reader_removed",
            source_id=source.id,
            payload={
                "telegram_account_id": str(account.id),
                "chat_id": source.chat_id,
                "source_revoked": source_revoked,
                "remaining_reader_count": len(remaining_reader_ids),
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
    def _reader_source_ids(session: Session, telegram_account_id: UUID) -> set[UUID]:
        rows = session.execute(
            text(
                """
                SELECT source_id
                FROM source_reader_access
                WHERE telegram_account_id = :telegram_account_id
                """
            ),
            {"telegram_account_id": telegram_account_id},
        ).scalars()
        return set(rows)

    @staticmethod
    def _source_reader_ids(session: Session, source_id: UUID) -> list[UUID]:
        rows = session.execute(
            text(
                """
                SELECT telegram_account_id
                FROM source_reader_access
                WHERE source_id = :source_id
                ORDER BY created_at ASC, telegram_account_id ASC
                """
            ),
            {"source_id": source_id},
        ).scalars()
        return list(rows)

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
        # Telegram sessions are private credentials. Even the platform owner may only
        # operate reader sessions that they personally connected.
        account = session.scalar(
            select(TelegramAccount).where(
                TelegramAccount.id == account_id,
                TelegramAccount.owner_user_id == actor["id"],
            )
        )
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
