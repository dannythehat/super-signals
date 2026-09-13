"""Continuous Telegram listener for exact selected Super Signals sources.

Day 12 deliberately stops at ingestion. New messages from selected sources in
TESTING, SHADOW or LIVE state are written to the internal Message log. PAUSED/revoked
sources and every unselected chat are ignored. No parsing, signal creation or
trade execution happens here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from telethon import TelegramClient, events
from telethon.sessions import StringSession

from app.models import Message, Source
from app.telegram_crypto import TelegramSessionCipher
from app.weekend_trading_freeze import market_week_frozen

LISTENABLE_SOURCE_STATES = {"testing", "shadow", "live"}


@dataclass(frozen=True, slots=True)
class ListeningSource:
    source_id: UUID
    chat_id: int
    title: str


@dataclass(frozen=True, slots=True)
class ReaderListeningPlan:
    telegram_account_id: UUID
    session_ciphertext: bytes
    sources: tuple[ListeningSource, ...]


@dataclass(frozen=True, slots=True)
class CapturedTelegramMessage:
    source_id: UUID
    chat_id: int
    telegram_message_id: int
    raw_text: str
    posted_at: datetime
    reply_to_message_id: int | None
    has_media: bool
    media_type: str | None


class TelegramListenerManager:
    """Keep Telethon workers aligned with the currently listenable source plan."""

    def __init__(
        self,
        *,
        api_id: int,
        api_hash: str,
        cipher: TelegramSessionCipher,
        session_factory: sessionmaker[Session],
        refresh_seconds: int = 5,
    ) -> None:
        if refresh_seconds <= 0:
            raise ValueError("Telegram listener refresh interval must be positive.")
        self._api_id = api_id
        self._api_hash = api_hash
        self._cipher = cipher
        self._session_factory = session_factory
        self._refresh_seconds = refresh_seconds
        self._manager_task: asyncio.Task[None] | None = None
        self._workers: dict[UUID, asyncio.Task[None]] = {}
        self._plan_signatures: dict[UUID, tuple[tuple[UUID, int], ...]] = {}
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._manager_task is not None and not self._manager_task.done():
            return
        self._stopping.clear()
        self._manager_task = asyncio.create_task(
            self._run(),
            name="super-signals-telegram-listener-manager",
        )

    async def stop(self) -> None:
        self._stopping.set()
        if self._manager_task is not None:
            self._manager_task.cancel()
            await self._await_cancelled(self._manager_task)
            self._manager_task = None
        await self._stop_all_workers()

    async def _run(self) -> None:
        try:
            while not self._stopping.is_set():
                try:
                    plan = await asyncio.to_thread(self._load_plan)
                    await self._reconcile(plan)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A transient database/Telegram failure must not terminate the API.
                    # The manager retries on the next reconciliation interval.
                    pass

                try:
                    await asyncio.wait_for(
                        self._stopping.wait(), timeout=self._refresh_seconds
                    )
                except TimeoutError:
                    pass
        finally:
            await self._stop_all_workers()

    def _load_plan(self) -> dict[UUID, ReaderListeningPlan]:
        """Choose exactly one connected private reader for every listenable source.

        During the canonical weekly XAU/USD market closure, return an empty plan.
        Reconciliation then disconnects every Telethon worker, so provider groups are
        not watched and no weekend Telegram traffic can reach persistence or AI.
        """
        if market_week_frozen():
            return {}

        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT
                        s.id AS source_id,
                        s.chat_id,
                        COALESCE(s.chat_title, s.source_alias) AS title,
                        s.telegram_account_id AS preferred_reader_id,
                        sra.telegram_account_id AS reader_id,
                        ta.session_ciphertext,
                        ta.status AS reader_status,
                        sra.created_at AS reader_link_created_at
                    FROM sources AS s
                    JOIN source_reader_access AS sra
                      ON sra.source_id = s.id
                    JOIN telegram_accounts AS ta
                      ON ta.id = sra.telegram_account_id
                    WHERE s.status IN ('testing', 'shadow', 'live')
                    ORDER BY
                        s.created_at ASC,
                        s.id ASC,
                        CASE WHEN sra.telegram_account_id = s.telegram_account_id THEN 0 ELSE 1 END,
                        sra.created_at ASC,
                        sra.telegram_account_id ASC
                    """
                )
            ).mappings()

            chosen_by_source: dict[UUID, tuple[UUID, bytes, ListeningSource]] = {}
            for row in rows:
                if row["reader_status"] != "connected":
                    continue
                source_id = row["source_id"]
                if source_id in chosen_by_source:
                    continue
                chosen_by_source[source_id] = (
                    row["reader_id"],
                    bytes(row["session_ciphertext"]),
                    ListeningSource(
                        source_id=source_id,
                        chat_id=int(row["chat_id"]),
                        title=str(row["title"]),
                    ),
                )

            grouped: dict[UUID, list[ListeningSource]] = {}
            ciphertexts: dict[UUID, bytes] = {}
            for reader_id, ciphertext, source in chosen_by_source.values():
                grouped.setdefault(reader_id, []).append(source)
                ciphertexts[reader_id] = ciphertext

            return {
                reader_id: ReaderListeningPlan(
                    telegram_account_id=reader_id,
                    session_ciphertext=ciphertexts[reader_id],
                    sources=tuple(
                        sorted(
                            sources,
                            key=lambda item: (item.chat_id, str(item.source_id)),
                        )
                    ),
                )
                for reader_id, sources in grouped.items()
            }

    async def _reconcile(self, plan: dict[UUID, ReaderListeningPlan]) -> None:
        desired_ids = set(plan)
        existing_ids = set(self._workers)

        for reader_id in existing_ids - desired_ids:
            await self._stop_worker(reader_id)

        for reader_id, reader_plan in plan.items():
            signature = tuple(
                (source.source_id, source.chat_id) for source in reader_plan.sources
            )
            worker = self._workers.get(reader_id)
            needs_restart = (
                worker is None
                or worker.done()
                or self._plan_signatures.get(reader_id) != signature
            )
            if not needs_restart:
                continue
            if worker is not None:
                await self._stop_worker(reader_id)
            self._plan_signatures[reader_id] = signature
            self._workers[reader_id] = asyncio.create_task(
                self._run_reader(reader_plan),
                name=f"super-signals-telegram-reader-{reader_id}",
            )

    async def _run_reader(self, plan: ReaderListeningPlan) -> None:
        session_string = self._cipher.decrypt(plan.session_ciphertext)
        client = TelegramClient(
            StringSession(session_string),
            self._api_id,
            self._api_hash,
            device_model="Super Signals Listener",
            app_version="0.7",
            system_lang_code="en",
            lang_code="en",
        )
        client.session.save_entities = False

        source_by_chat_id = {source.chat_id: source for source in plan.sources}
        exact_chat_ids = tuple(source_by_chat_id)

        async def handle_new_message(event: Any) -> None:
            chat_id = getattr(event, "chat_id", None)
            if chat_id is None:
                return
            source = source_by_chat_id.get(int(chat_id))
            if source is None:
                return

            message = getattr(event, "message", None)
            message_id = getattr(message, "id", None)
            if message_id is None:
                return
            posted_at = getattr(message, "date", None)
            if not isinstance(posted_at, datetime):
                posted_at = datetime.now(UTC)
            elif posted_at.tzinfo is None:
                posted_at = posted_at.replace(tzinfo=UTC)
            else:
                posted_at = posted_at.astimezone(UTC)

            reply_to = getattr(message, "reply_to", None)
            reply_to_message_id = getattr(reply_to, "reply_to_msg_id", None)
            media = getattr(message, "media", None)
            captured = CapturedTelegramMessage(
                source_id=source.source_id,
                chat_id=source.chat_id,
                telegram_message_id=int(message_id),
                raw_text=str(getattr(event, "raw_text", "") or ""),
                posted_at=posted_at,
                reply_to_message_id=(
                    int(reply_to_message_id)
                    if reply_to_message_id is not None
                    else None
                ),
                has_media=media is not None,
                media_type=type(media).__name__ if media is not None else None,
            )
            await asyncio.to_thread(self._persist_message, captured)

        try:
            await client.connect()
            if not await client.is_user_authorized():
                return
            client.add_event_handler(
                handle_new_message,
                events.NewMessage(chats=list(exact_chat_ids)),
            )
            await client.run_until_disconnected()
        finally:
            if client.is_connected():
                await client.disconnect()

    def _persist_message(self, captured: CapturedTelegramMessage) -> bool:
        """Persist one raw Telegram message and nothing further down the pipeline."""
        if market_week_frozen(captured.posted_at):
            return False

        with self._session_factory() as session:
            source = session.scalar(
                select(Source).where(
                    Source.id == captured.source_id,
                    Source.chat_id == captured.chat_id,
                    Source.status.in_(LISTENABLE_SOURCE_STATES),
                )
            )
            if source is None:
                return False

            raw_text = captured.raw_text
            session.add(
                Message(
                    source_id=captured.source_id,
                    telegram_message_id=captured.telegram_message_id,
                    raw_text=raw_text,
                    raw_payload={
                        "chat_id": captured.chat_id,
                        "reply_to_message_id": captured.reply_to_message_id,
                        "has_media": captured.has_media,
                        "media_type": captured.media_type,
                    },
                    posted_at=captured.posted_at,
                    ingestion_status="received",
                    content_sha256=sha256(raw_text.encode("utf-8")).hexdigest(),
                )
            )
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return False
            return True

    async def _stop_worker(self, reader_id: UUID) -> None:
        worker = self._workers.pop(reader_id, None)
        self._plan_signatures.pop(reader_id, None)
        if worker is None:
            return
        worker.cancel()
        await self._await_cancelled(worker)

    async def _stop_all_workers(self) -> None:
        for reader_id in tuple(self._workers):
            await self._stop_worker(reader_id)

    @staticmethod
    async def _await_cancelled(task: asyncio.Task[None]) -> None:
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            # A failed reader is intentionally retried by the reconciliation loop.
            pass


def build_listener_manager(
    *,
    api_id: int,
    api_hash: str,
    cipher: TelegramSessionCipher,
    session_factory: sessionmaker[Session],
    refresh_seconds: int,
) -> TelegramListenerManager:
    return TelegramListenerManager(
        api_id=api_id,
        api_hash=api_hash,
        cipher=cipher,
        session_factory=session_factory,
        refresh_seconds=refresh_seconds,
    )