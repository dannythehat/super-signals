"""Provider Lab discovery, style learning and duplicate evidence for joined Telegram sources.

The research scanner is deliberately non-executing. It samples only chats already visible
to connected Telegram readers, creates newly discovered Gold/XAUUSD candidates as SHADOW,
and records bounded communication-style evidence. Historical samples are never inserted
into the live message pipeline and therefore can never be replayed as trades.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app.models import AuditEvent, Source, TelegramAccount
from app.telegram_crypto import SessionDecryptionError, TelegramSessionCipher
from app.telegram_source_gateway import (
    TelegramResearchDialog,
    TelegramResearchMessage,
    TelegramSourceGatewayError,
    TelethonTelegramSourceGateway,
)

logger = logging.getLogger(__name__)

_GOLD = re.compile(r"\b(?:xau\s*/?\s*usd|xauusd|xau|gold)\b", re.IGNORECASE)
_SIDE = re.compile(r"\b(?:buy|sell|long|short)\b", re.IGNORECASE)
_STOP = re.compile(r"\b(?:sl|stop\s*loss|stoploss)\b", re.IGNORECASE)
_TARGET = re.compile(r"\b(?:tp\s*\d*|take\s*profit|target\s*\d*)\b", re.IGNORECASE)
_ENTRY = re.compile(r"\b(?:entry|buy\s+now|sell\s+now|buy\s+limit|sell\s+limit|buy\s+stop|sell\s+stop)\b", re.IGNORECASE)
_MANAGEMENT = re.compile(
    r"\b(?:tp\s*\d*\s*(?:hit|done|reached)|sl\s*(?:hit|done)|break\s*even|breakeven|\bbe\b|risk\s*free|close\s*(?:half|partial|all|now)?|partial(?:s)?|runner|trail(?:ing)?|secure\s+profit|cancel\s+(?:limit|order|pending))\b",
    re.IGNORECASE,
)
_PENDING = re.compile(r"\b(?:buy|sell)\s+(?:limit|stop)\b", re.IGNORECASE)
_SCALP = re.compile(r"\bscalp(?:er|ers|ing)?\b", re.IGNORECASE)
_SWING = re.compile(r"\bswing\b", re.IGNORECASE)
_TITLE_TRADING = re.compile(r"\b(?:signal(?:s)?|trade(?:r|rs|s|ing)?|scalp(?:er|ing)?)\b", re.IGNORECASE)
_URL = re.compile(r"https?://\S+|t\.me/\S+", re.IGNORECASE)
_SPACE = re.compile(r"\s+")
_NON_CORE = re.compile(r"[^a-z0-9.+@%:/_-]+")


@dataclass(frozen=True, slots=True)
class ProviderSampleStats:
    title: str
    candidate: bool
    style: str
    observed_messages: int
    signal_like_messages: int
    structured_signal_messages: int
    management_messages: int
    edited_messages: int
    pending_messages: int
    signal_likelihood: float
    interpretation_readiness: float
    fingerprints: frozenset[str]
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ProviderResearchScanSummary:
    readers_scanned: int
    dialogs_sampled: int
    candidates_found: int
    new_shadow_sources: int
    profiles_updated: int
    duplicate_flags: int
    scan_failures: int


def _signal_like(text_value: str) -> bool:
    text_value = text_value.strip()
    if not text_value:
        return False
    gold = _GOLD.search(text_value) is not None
    side = _SIDE.search(text_value) is not None
    protection = _STOP.search(text_value) is not None
    target = _TARGET.search(text_value) is not None
    entry = _ENTRY.search(text_value) is not None
    return (gold and side) or (side and protection and target) or (entry and protection and target)


def _structured_signal(text_value: str) -> bool:
    return (
        _SIDE.search(text_value) is not None
        and _STOP.search(text_value) is not None
        and _TARGET.search(text_value) is not None
    )


def _fingerprint(text_value: str) -> str:
    normalized = _URL.sub(" ", text_value.casefold())
    normalized = _NON_CORE.sub(" ", normalized)
    normalized = _SPACE.sub(" ", normalized).strip()
    return sha256(normalized.encode("utf-8")).hexdigest()


def analyze_provider_sample(dialog: TelegramResearchDialog) -> ProviderSampleStats:
    messages = tuple(message for message in dialog.messages if message.raw_text.strip())
    signal_messages = tuple(message for message in messages if _signal_like(message.raw_text))
    structured = sum(_structured_signal(message.raw_text) for message in signal_messages)
    management = sum(_MANAGEMENT.search(message.raw_text) is not None for message in messages)
    edited = sum(message.edited_at is not None for message in messages)
    pending = sum(_PENDING.search(message.raw_text) is not None for message in signal_messages)
    observed = len(messages)
    signal_count = len(signal_messages)

    title_gold = _GOLD.search(dialog.title) is not None
    title_trading = _TITLE_TRADING.search(dialog.title) is not None
    sample_gold = sum(_GOLD.search(message.raw_text) is not None for message in signal_messages)
    candidate = bool(
        (title_gold and (title_trading or signal_count >= 1))
        or (signal_count >= 2 and sample_gold >= 1)
    )

    if signal_count:
        dates = [message.posted_at for message in signal_messages]
        span_seconds = max(0.0, (max(dates) - min(dates)).total_seconds()) if len(dates) > 1 else 0.0
        span_days = max(1.0, span_seconds / 86400.0)
        signals_per_day = signal_count / span_days
    else:
        signals_per_day = 0.0

    sample_text = "\n".join(message.raw_text for message in messages)
    scalp_language = _SCALP.search(dialog.title) is not None or _SCALP.search(sample_text) is not None
    swing_language = _SWING.search(dialog.title) is not None or _SWING.search(sample_text) is not None
    if scalp_language or signals_per_day >= 5.0:
        style = "scalper"
    elif swing_language and signals_per_day < 2.0:
        style = "swing_or_sparse"
    elif signals_per_day >= 1.5:
        style = "intraday"
    elif signal_count and signals_per_day < 0.75:
        style = "swing_or_sparse"
    elif signal_count:
        style = "mixed"
    else:
        style = "unknown"

    if observed:
        density = signal_count / observed
    else:
        density = 0.0
    structure_rate = structured / signal_count if signal_count else 0.0
    management_rate = min(1.0, management / max(signal_count, 1))
    sample_strength = min(1.0, signal_count / 12.0)
    title_strength = 0.25 if title_gold and title_trading else (0.12 if title_gold else 0.0)
    signal_likelihood = min(1.0, title_strength + 0.45 * min(1.0, density * 3.0) + 0.30 * sample_strength)
    readiness = min(
        0.99,
        0.10 + 0.55 * structure_rate + 0.20 * management_rate + 0.14 * sample_strength,
    ) if candidate else 0.0

    fingerprints = frozenset(_fingerprint(message.raw_text) for message in signal_messages)
    return ProviderSampleStats(
        title=dialog.title,
        candidate=candidate,
        style=style,
        observed_messages=observed,
        signal_like_messages=signal_count,
        structured_signal_messages=structured,
        management_messages=management,
        edited_messages=edited,
        pending_messages=pending,
        signal_likelihood=round(signal_likelihood, 5),
        interpretation_readiness=round(readiness, 5),
        fingerprints=fingerprints,
        metadata={
            "sample_signal_density": round(density, 5),
            "sample_signals_per_day": round(signals_per_day, 3),
            "title_gold_hint": title_gold,
            "title_trading_hint": title_trading,
            "sample_gold_signal_messages": sample_gold,
            "pending_signal_messages": pending,
            "scalp_language": scalp_language,
            "swing_language": swing_language,
            "historical_sample_traded": False,
        },
    )


def duplicate_similarity(left: ProviderSampleStats, right: ProviderSampleStats) -> float:
    if len(left.fingerprints) < 3 or len(right.fingerprints) < 3:
        return 0.0
    overlap = len(left.fingerprints & right.fingerprints)
    if overlap < 3:
        return 0.0
    return round(overlap / min(len(left.fingerprints), len(right.fingerprints)), 5)


class ProviderResearchService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        cipher: TelegramSessionCipher,
        gateway: TelethonTelegramSourceGateway,
        history_limit: int = 30,
    ) -> None:
        self._session_factory = session_factory
        self._cipher = cipher
        self._gateway = gateway
        self._history_limit = min(max(int(history_limit), 5), 100)

    async def scan_once(self) -> ProviderResearchScanSummary:
        accounts = self._connected_accounts()
        sampled: dict[int, tuple[UUID, UUID, TelegramResearchDialog, ProviderSampleStats]] = {}
        failures = 0
        dialogs_sampled = 0

        for account_id, owner_user_id, ciphertext in accounts:
            try:
                session_string = self._cipher.decrypt(ciphertext)
                dialogs = await self._gateway.scan_joined_research_dialogs(
                    session_string,
                    history_limit=self._history_limit,
                )
            except (SessionDecryptionError, TelegramSourceGatewayError, Exception) as exc:
                failures += 1
                logger.warning(
                    "Provider Lab reader scan failed account=%s code=%s",
                    account_id,
                    type(exc).__name__,
                )
                continue

            dialogs_sampled += len(dialogs)
            for dialog in dialogs:
                stats = analyze_provider_sample(dialog)
                if not stats.candidate:
                    continue
                sampled.setdefault(dialog.chat_id, (account_id, owner_user_id, dialog, stats))

        new_shadow = 0
        profile_rows: list[tuple[UUID, ProviderSampleStats]] = []
        with self._session_factory() as session:
            for account_id, owner_user_id, dialog, stats in sampled.values():
                source, created = self._ensure_shadow_source(
                    session,
                    account_id=account_id,
                    owner_user_id=owner_user_id,
                    dialog=dialog,
                )
                if source is None:
                    continue
                new_shadow += int(created)
                profile_rows.append((source.id, stats))
            session.commit()

        # The live Telegram scan is useful for discovery, but it must not be the only
        # learning source. Production already stores the full provider message stream;
        # use that durable evidence as a fallback/richer sample so a temporarily sparse
        # Telegram scan can never reset an established provider to zero readiness.
        stored_rows = self._stored_profile_rows()
        richest: dict[UUID, ProviderSampleStats] = {source_id: stats for source_id, stats in profile_rows}
        for source_id, stats in stored_rows:
            current = richest.get(source_id)
            if current is None or (
                stats.observed_messages,
                stats.structured_signal_messages,
                stats.signal_like_messages,
            ) > (
                current.observed_messages,
                current.structured_signal_messages,
                current.signal_like_messages,
            ):
                richest[source_id] = stats
        profile_rows = list(richest.items())

        duplicates = self._duplicate_map(profile_rows)
        with self._session_factory() as session:
            for source_id, stats in profile_rows:
                duplicate_of, duplicate_score = duplicates.get(source_id, (None, None))
                research_state = "duplicate_review" if duplicate_of is not None else "learning"
                metadata = dict(stats.metadata)
                metadata["sample_fingerprint_count"] = len(stats.fingerprints)
                session.execute(
                    text(
                        """
                        INSERT INTO provider_research_profiles(
                            source_id,research_state,style,observed_messages,signal_like_messages,
                            structured_signal_messages,management_messages,edited_messages,
                            signal_likelihood,interpretation_readiness,duplicate_of_source_id,
                            duplicate_score,profile_metadata,last_scan_at,updated_at
                        ) VALUES (
                            :source_id,:research_state,:style,:observed,:signal_like,:structured,
                            :management,:edited,:likelihood,:readiness,:duplicate_of,
                            :duplicate_score,CAST(:metadata AS jsonb),now(),now()
                        )
                        ON CONFLICT (source_id) DO UPDATE SET
                            research_state=CASE
                                WHEN provider_research_profiles.research_state IN ('shadow','qualified','rejected')
                                THEN provider_research_profiles.research_state
                                ELSE EXCLUDED.research_state
                            END,
                            style=EXCLUDED.style,
                            observed_messages=EXCLUDED.observed_messages,
                            signal_like_messages=EXCLUDED.signal_like_messages,
                            structured_signal_messages=EXCLUDED.structured_signal_messages,
                            management_messages=EXCLUDED.management_messages,
                            edited_messages=EXCLUDED.edited_messages,
                            signal_likelihood=EXCLUDED.signal_likelihood,
                            interpretation_readiness=EXCLUDED.interpretation_readiness,
                            duplicate_of_source_id=EXCLUDED.duplicate_of_source_id,
                            duplicate_score=EXCLUDED.duplicate_score,
                            profile_metadata=EXCLUDED.profile_metadata,
                            last_scan_at=now(),updated_at=now()
                        """
                    ),
                    {
                        "source_id": source_id,
                        "research_state": research_state,
                        "style": stats.style,
                        "observed": stats.observed_messages,
                        "signal_like": stats.signal_like_messages,
                        "structured": stats.structured_signal_messages,
                        "management": stats.management_messages,
                        "edited": stats.edited_messages,
                        "likelihood": stats.signal_likelihood,
                        "readiness": stats.interpretation_readiness,
                        "duplicate_of": duplicate_of,
                        "duplicate_score": duplicate_score,
                        "metadata": json.dumps(metadata, sort_keys=True),
                    },
                )
            session.commit()

        return ProviderResearchScanSummary(
            readers_scanned=len(accounts),
            dialogs_sampled=dialogs_sampled,
            candidates_found=len(sampled),
            new_shadow_sources=new_shadow,
            profiles_updated=len(profile_rows),
            duplicate_flags=len(duplicates),
            scan_failures=failures,
        )

    def _stored_profile_rows(self) -> list[tuple[UUID, ProviderSampleStats]]:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT s.id AS source_id,s.chat_id,
                           COALESCE(NULLIF(s.chat_title,''),NULLIF(s.source_alias,''),'Provider') AS title,
                           m.telegram_message_id,m.raw_text,m.posted_at,m.edited_at
                    FROM sources s
                    JOIN LATERAL (
                        SELECT mm.telegram_message_id,mm.raw_text,mm.posted_at,mm.edited_at
                        FROM messages mm
                        WHERE mm.source_id=s.id
                          AND COALESCE(mm.raw_text,'')<>''
                        ORDER BY mm.posted_at DESC
                        LIMIT :history_limit
                    ) m ON true
                    WHERE s.status IN ('testing','shadow','live','active')
                    ORDER BY s.id,m.posted_at
                    """
                ),
                {"history_limit": self._history_limit},
            ).mappings().all()

        grouped: dict[UUID, dict[str, Any]] = {}
        for row in rows:
            source_id = UUID(str(row["source_id"]))
            item = grouped.setdefault(
                source_id,
                {
                    "chat_id": int(row["chat_id"]),
                    "title": str(row["title"]),
                    "messages": [],
                },
            )
            item["messages"].append(
                TelegramResearchMessage(
                    telegram_message_id=int(row["telegram_message_id"]),
                    raw_text=str(row["raw_text"] or ""),
                    posted_at=row["posted_at"],
                    edited_at=row["edited_at"],
                )
            )

        result: list[tuple[UUID, ProviderSampleStats]] = []
        for source_id, item in grouped.items():
            dialog = TelegramResearchDialog(
                chat_id=item["chat_id"],
                title=item["title"],
                kind="channel",
                messages=tuple(item["messages"]),
            )
            result.append((source_id, analyze_provider_sample(dialog)))
        return result

    def _connected_accounts(self) -> list[tuple[UUID, UUID, bytes]]:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT id,owner_user_id,session_ciphertext
                    FROM telegram_accounts
                    WHERE status='connected'
                    ORDER BY created_at,id
                    """
                )
            ).mappings().all()
        return [
            (UUID(str(row["id"])), UUID(str(row["owner_user_id"])), bytes(row["session_ciphertext"]))
            for row in rows
        ]

    @staticmethod
    def _ensure_reader_access(
        session: Session,
        *,
        source_id: UUID,
        account_id: UUID,
        owner_user_id: UUID,
    ) -> None:
        session.execute(
            text(
                """
                INSERT INTO source_reader_access(source_id,telegram_account_id,created_by_user_id)
                VALUES (:source_id,:account_id,:owner_user_id)
                ON CONFLICT (source_id,telegram_account_id) DO NOTHING
                """
            ),
            {
                "source_id": source_id,
                "account_id": account_id,
                "owner_user_id": owner_user_id,
            },
        )

    def _ensure_shadow_source(
        self,
        session: Session,
        *,
        account_id: UUID,
        owner_user_id: UUID,
        dialog: TelegramResearchDialog,
    ) -> tuple[Source | None, bool]:
        source = session.scalar(
            select(Source)
            .where(Source.chat_id == dialog.chat_id)
            .order_by(Source.created_at.asc())
        )
        if source is not None and source.status == "revoked":
            return None, False

        created = source is None
        if source is None:
            source = Source(
                telegram_account_id=account_id,
                chat_id=dialog.chat_id,
                chat_title=dialog.title[:255],
                source_alias=dialog.title[:120],
                status="shadow",
                redistribution_permission_confirmed=False,
                permission_notes="Provider Lab auto-enrolled from an already joined Telegram research source.",
                created_by_user_id=owner_user_id,
            )
            session.add(source)
            session.flush()
            session.add(
                AuditEvent(
                    actor_user_id=owner_user_id,
                    event_type="telegram.provider_lab_auto_shadowed",
                    entity_type="source",
                    entity_id=source.id,
                    payload={
                        "chat_id": dialog.chat_id,
                        "source_title": dialog.title,
                        "status": "shadow",
                        "research_import": True,
                        "historical_sample_traded": False,
                        "live_trading_enabled": False,
                    },
                )
            )
        else:
            source.chat_title = dialog.title[:255]

        self._ensure_reader_access(
            session,
            source_id=source.id,
            account_id=account_id,
            owner_user_id=owner_user_id,
        )
        return source, created

    @staticmethod
    def _duplicate_map(
        rows: list[tuple[UUID, ProviderSampleStats]],
    ) -> dict[UUID, tuple[UUID, float]]:
        flagged: dict[UUID, tuple[UUID, float]] = {}
        for index, (left_id, left) in enumerate(rows):
            for right_id, right in rows[index + 1 :]:
                score = duplicate_similarity(left, right)
                if score < 0.60:
                    continue
                existing = flagged.get(right_id)
                if existing is None or score > existing[1]:
                    flagged[right_id] = (left_id, score)
        return flagged


class ProviderResearchManager:
    def __init__(self, service: ProviderResearchService, *, scan_seconds: int = 21600) -> None:
        self._service = service
        self._scan_seconds = max(900, int(scan_seconds))
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="super-signals-provider-research")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                summary = await self._service.scan_once()
                logger.info(
                    "Provider Lab scan readers=%d sampled=%d candidates=%d new_shadow=%d profiles=%d duplicates=%d failures=%d",
                    summary.readers_scanned,
                    summary.dialogs_sampled,
                    summary.candidates_found,
                    summary.new_shadow_sources,
                    summary.profiles_updated,
                    summary.duplicate_flags,
                    summary.scan_failures,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Provider Lab scan failed safely")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._scan_seconds)
            except TimeoutError:
                pass


def build_provider_research_manager(
    *,
    session_factory: sessionmaker[Session],
    cipher: TelegramSessionCipher,
    gateway: TelethonTelegramSourceGateway,
) -> ProviderResearchManager:
    history_limit = int(os.getenv("SUPER_SIGNALS_PROVIDER_RESEARCH_HISTORY_LIMIT", "100") or "100")
    scan_seconds = int(os.getenv("SUPER_SIGNALS_PROVIDER_RESEARCH_SCAN_SECONDS", "1800") or "1800")
    service = ProviderResearchService(
        session_factory=session_factory,
        cipher=cipher,
        gateway=gateway,
        history_limit=history_limit,
    )
    return ProviderResearchManager(service, scan_seconds=scan_seconds)


__all__ = [
    "ProviderResearchManager",
    "ProviderResearchScanSummary",
    "ProviderResearchService",
    "ProviderSampleStats",
    "analyze_provider_sample",
    "build_provider_research_manager",
    "duplicate_similarity",
]
