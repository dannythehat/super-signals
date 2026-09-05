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
from typing import Any

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

    async def _recover_committed_dispatch_gaps(self) -> int:
        """Offer fresh committed decisions to the idempotent broker router after restart."""
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
                # Recover durable broker work before waiting on Telegram network startup.
                # This closes the commit->dispatch restart gap without replaying AI.
                await self._recover_committed_dispatch_gaps()
                await self._inner.start()
                inner_started = True
                await self._research.start()
                await self._refresh_adaptive_profiles()
                if self._adaptive_refresh_task is None or self._adaptive_refresh_task.done():
                    self._adaptive_refresh_task = asyncio.create_task(
                        self._adaptive_refresh_loop(),
                        name="super-signals-adaptive-provider-profile-refresh",
                    )
                logger.info("Production Telegram listener and adaptive Provider Lab started")
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
        try:
            await self._research.stop()
        finally:
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
