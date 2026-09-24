"""Production Telegram listener wiring.

There is one production ingress generation and one production semantic pipeline.
Production code must never select a day-numbered listener builder or install behaviour
at import time.

Safety invariant: if Telegram listening is enabled for production paper trading, the
canonical broker router must exist. A reader that records executable decisions without
a broker router is forbidden because it creates silent missed trades.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.benchmarking_shadow_lifecycle_bridge import BenchmarkingShadowAwareAiLifecycleBridge
from app.provider_aware_ai_pipeline import ProviderAwareProductionAiPipeline
from app.provider_research import ProviderResearchManager, build_provider_research_manager
from app.shadow_signal_ledger import ShadowAwareCanonicalSignalLedger
from app.telegram_listener_canonical import (
    CanonicalProductionTelegramListenerManager,
    build_canonical_production_listener_manager,
)
from app.telegram_source_gateway import TelethonTelegramSourceGateway

PRODUCTION_LISTENER_GENERATION = "canonical-v1"
PRODUCTION_AI_GENERATION = "provider-aware-v3-adaptive"
_ADAPTIVE_PROFILE_REFRESH_SECONDS = 900
_DISPATCH_GAP_SWEEP_SECONDS = 10

logger = logging.getLogger(__name__)


class ProviderResearchProductionListener(CanonicalProductionTelegramListenerManager):
    """Lifecycle wrapper: canonical ingress plus Provider Lab learning services.

    Telegram/Provider Lab recovery is deliberately outside FastAPI's HTTP startup
    critical path. A slow Telegram connection, a large recovery set, or one unreadable
    research channel must never prevent /health or the dashboard from opening.
    """

    def __init__(
        self,
        inner: CanonicalProductionTelegramListenerManager,
        research: ProviderResearchManager,
    ) -> None:
        self._inner = inner
        self._research = research
        self._startup_task: asyncio.Task[None] | None = None
        self._adaptive_refresh_task: asyncio.Task[None] | None = None
        self._dispatch_gap_task: asyncio.Task[None] | None = None
        self._research_enabled = (
            os.getenv("SUPER_SIGNALS_RESEARCH_LANE_ENABLED", "0").strip() == "1"
        )
        self._stopping = asyncio.Event()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def start(self) -> None:
        """Schedule Telegram startup/recovery and return immediately to FastAPI."""
        if self._startup_task is not None and not self._startup_task.done():
            return
        self._stopping.clear()
        self._startup_task = asyncio.create_task(
            self._start_with_retry(),
            name="super-signals-production-telegram-startup",
        )

    def _committed_dispatch_gap_rows(self) -> list[dict[str, Any]]:
        """Load recent durable actionable decisions without blocking the asyncio loop."""
        with self._inner._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    WITH latest AS (
                        SELECT DISTINCT ON (d.message_id,d.revision_index)
                            m.source_id,
                            m.telegram_message_id,
                            d.revision_index,
                            d.decision,
                            d.action,
                            src.status AS source_status,
                            COALESCE(mr.edited_at,m.posted_at,d.created_at) AS occurred_at,
                            d.created_at
                        FROM ai_message_decisions AS d
                        JOIN messages AS m ON m.id=d.message_id
                        JOIN sources AS src ON src.id=m.source_id
                        LEFT JOIN message_revisions AS mr
                          ON mr.message_id=d.message_id
                         AND mr.revision_index=d.revision_index
                        WHERE d.created_at >= now() - interval '15 minutes'
                        ORDER BY d.message_id,d.revision_index,d.created_at DESC
                    )
                    SELECT source_id,telegram_message_id,revision_index,occurred_at
                    FROM latest
                    WHERE source_status IN ('testing','shadow','live')
                      AND (
                            (decision='new_trade' AND action='execute')
                            OR
                            (decision='trade_update' AND action='apply_update')
                      )
                    ORDER BY created_at,telegram_message_id,revision_index
                    """
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    async def _recover_committed_dispatch_gaps(self) -> int:
        """Offer fresh committed decisions without blocking HTTP health checks."""
        rows = await asyncio.to_thread(self._committed_dispatch_gap_rows)

        checked = 0
        for row in rows:
            await self._inner._dispatch_recovered_if_required(
                source_id=row["source_id"],
                telegram_message_id=int(row["telegram_message_id"]),
                revision_index=int(row["revision_index"]),
                occurred_at=row["occurred_at"],
            )
            checked += 1

        if checked:
            logger.info(
                "Checked %d fresh committed decisions for restart dispatch gaps",
                checked,
            )
        return checked

    async def _dispatch_configured_management_replay(self) -> bool:
        """Replay one explicitly configured recent management message, then rely on idempotency.

        This is an operator break-glass path for a confirmed missed management instruction.
        It is disabled unless the environment variable is present, accepts management only,
        requires the exact provider/message/revision to exist in durable storage, and refuses
        to execute after the configured age window.  The canonical router remains responsible
        for target resolution, broker reconciliation and idempotency.
        """
        raw = os.getenv("SUPER_SIGNALS_ONE_TIME_MANAGEMENT_REPLAY", "").strip()
        if not raw:
            return False

        parts = [item.strip() for item in raw.split(":")]
        if len(parts) not in {3, 4}:
            logger.error("One-time management replay configuration malformed")
            return False
        try:
            source_id = UUID(parts[0])
            telegram_message_id = int(parts[1])
            revision_index = int(parts[2])
            max_age_seconds = float(parts[3]) if len(parts) == 4 else 1800.0
        except (ValueError, TypeError):
            logger.error("One-time management replay configuration invalid")
            return False
        if telegram_message_id <= 0 or revision_index < 0 or not 0 < max_age_seconds <= 3600:
            logger.error("One-time management replay bounds invalid")
            return False

        with self._inner._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT
                        d.decision,
                        d.action,
                        src.status AS source_status,
                        COALESCE(mr.edited_at,m.posted_at,d.created_at) AS occurred_at
                    FROM messages AS m
                    JOIN sources AS src ON src.id=m.source_id
                    JOIN ai_message_decisions AS d
                      ON d.message_id=m.id
                     AND d.revision_index=:revision_index
                    LEFT JOIN message_revisions AS mr
                      ON mr.message_id=m.id
                     AND mr.revision_index=:revision_index
                    WHERE m.source_id=:source_id
                      AND m.telegram_message_id=:telegram_message_id
                      AND m.deleted_at IS NULL
                      AND (:revision_index=0 OR mr.revision_index IS NOT NULL)
                    LIMIT 1
                    """
                ),
                {
                    "source_id": source_id,
                    "telegram_message_id": telegram_message_id,
                    "revision_index": revision_index,
                },
            ).mappings().first()

        if row is None:
            logger.error("One-time management replay durable message not found")
            return False
        if str(row["source_status"] or "") not in {"testing", "live"}:
            logger.error("One-time management replay source is not broker-eligible")
            return False
        if str(row["decision"] or "") != "trade_update" or str(row["action"] or "") != "apply_update":
            logger.error("One-time management replay refused non-management decision")
            return False

        occurred_at = row["occurred_at"]
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=UTC)
        age_seconds = (datetime.now(UTC) - occurred_at.astimezone(UTC)).total_seconds()
        if age_seconds < -5 or age_seconds > max_age_seconds:
            logger.error(
                "One-time management replay expired age_seconds=%.1f max_age_seconds=%.1f",
                age_seconds,
                max_age_seconds,
            )
            return False

        router = self._inner._canonical_router
        if router is None:
            raise RuntimeError("one_time_management_replay_router_missing")
        result = await router.dispatch_stored_decision(
            source_id=source_id,
            telegram_message_id=telegram_message_id,
            revision_index=revision_index,
        )
        logger.warning(
            "One-time management replay outcome=%s message=%s revision=%s broker_actions=%s reason=%s",
            result.outcome,
            telegram_message_id,
            revision_index,
            result.broker_actions_sent,
            result.error_code or result.reason,
        )
        if result.outcome == "blocked":
            raise RuntimeError(
                f"one_time_management_replay_blocked:{result.error_code or result.reason}"
            )
        return True

    async def _dispatch_gap_loop(self) -> None:
        """Continuously close fresh commit->dispatch gaps without replaying stale entries."""
        while not self._stopping.is_set():
            try:
                await self._recover_committed_dispatch_gaps()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Fresh broker dispatch gap sweep failed safely")
            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=_DISPATCH_GAP_SWEEP_SECONDS,
                )
            except TimeoutError:
                pass

    async def _refresh_adaptive_profiles(self) -> None:
        pipeline = getattr(self._inner, "_ai_pipeline", None)
        refresh = getattr(pipeline, "refresh_all_provider_profiles", None)
        if callable(refresh):
            count = await asyncio.to_thread(refresh)
            logger.info("Adaptive Provider Lab profiles refreshed for %d sources", count)

    async def _adaptive_refresh_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=_ADAPTIVE_PROFILE_REFRESH_SECONDS,
                )
                return
            except TimeoutError:
                pass
            try:
                await self._refresh_adaptive_profiles()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Adaptive provider profile refresh failed safely")

    async def _start_with_retry(self) -> None:
        while not self._stopping.is_set():
            inner_started = False
            try:
                # A configured operator-approved management replay runs before Telegram
                # network startup so a confirmed missed protective instruction is not
                # delayed by provider recovery or Telethon connectivity.
                await self._dispatch_configured_management_replay()
                # Recover durable broker work before waiting on Telegram network startup.
                # This closes the commit->dispatch restart gap without replaying AI.
                await self._recover_committed_dispatch_gaps()
                if self._dispatch_gap_task is None or self._dispatch_gap_task.done():
                    self._dispatch_gap_task = asyncio.create_task(
                        self._dispatch_gap_loop(),
                        name="super-signals-fresh-dispatch-gap-sweep",
                    )
                await self._inner.start()
                inner_started = True
                if self._research_enabled:
                    await self._research.start()
                    await self._refresh_adaptive_profiles()
                    if self._adaptive_refresh_task is None or self._adaptive_refresh_task.done():
                        self._adaptive_refresh_task = asyncio.create_task(
                            self._adaptive_refresh_loop(),
                            name="super-signals-adaptive-provider-profile-refresh",
                        )
                    logger.info("Production Telegram listener and adaptive Provider Lab started")
                else:
                    logger.warning(
                        "Production Telegram listener started with provider research disabled; "
                        "live execution and publication only"
                    )
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Production Telegram startup failed safely; web remains available and startup will retry"
                )
                if inner_started:
                    try:
                        await self._inner.stop()
                    except Exception:
                        logger.exception("Telegram partial-start cleanup failed safely")
                if self._stopping.is_set():
                    return
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=5)
                except TimeoutError:
                    pass

    async def stop(self) -> None:
        self._stopping.set()
        dispatch_gap_task = self._dispatch_gap_task
        if dispatch_gap_task is not None and not dispatch_gap_task.done():
            dispatch_gap_task.cancel()
            try:
                await dispatch_gap_task
            except asyncio.CancelledError:
                pass
        self._dispatch_gap_task = None

        refresh_task = self._adaptive_refresh_task
        if refresh_task is not None and not refresh_task.done():
            refresh_task.cancel()
            try:
                await refresh_task
            except asyncio.CancelledError:
                pass
        self._adaptive_refresh_task = None

        startup_task = self._startup_task
        if startup_task is not None and not startup_task.done():
            startup_task.cancel()
            try:
                await startup_task
            except asyncio.CancelledError:
                pass
        self._startup_task = None
        if self._research_enabled:
            try:
                await self._research.stop()
            finally:
                await self._inner.stop()
        else:
            await self._inner.stop()


def build_production_listener_manager(**kwargs: Any) -> CanonicalProductionTelegramListenerManager:
    manager = build_canonical_production_listener_manager(**kwargs)
    if not isinstance(manager, CanonicalProductionTelegramListenerManager):
        raise RuntimeError("production_listener_generation_mismatch")
    if getattr(manager, "_canonical_router", None) is None:
        raise RuntimeError("production_execution_router_missing")

    existing_pipeline = getattr(manager, "_ai_pipeline", None)
    if existing_pipeline is not None:
        pipeline = ProviderAwareProductionAiPipeline(
            session_factory=manager._session_factory,
            supervisor=getattr(existing_pipeline, "_supervisor", None),
        )
        pipeline._signals = ShadowAwareCanonicalSignalLedger(manager._session_factory)
        pipeline._lifecycle = BenchmarkingShadowAwareAiLifecycleBridge(manager._session_factory)
        manager._ai_pipeline = pipeline

    api_id = kwargs.get("api_id")
    api_hash = kwargs.get("api_hash")
    cipher = kwargs.get("cipher")
    session_factory = kwargs.get("session_factory")
    if api_id is None or not api_hash or cipher is None or session_factory is None:
        return manager

    research = build_provider_research_manager(
        session_factory=session_factory,
        cipher=cipher,
        gateway=TelethonTelegramSourceGateway(int(api_id), str(api_hash)),
    )
    return ProviderResearchProductionListener(manager, research)


__all__ = [
    "PRODUCTION_AI_GENERATION",
    "PRODUCTION_LISTENER_GENERATION",
    "ProviderResearchProductionListener",
    "build_production_listener_manager",
]
