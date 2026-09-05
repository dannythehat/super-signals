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
from typing import Any

from app.canonical_signal_ledger import CanonicalSignalLedger
from app.provider_aware_ai_pipeline import ProviderAwareProductionAiPipeline
from app.provider_research import ProviderResearchManager, build_provider_research_manager
from app.shadow_lifecycle_bridge import ShadowAwareAiLifecycleBridge
from app.telegram_listener_canonical import (
    CanonicalProductionTelegramListenerManager,
    build_canonical_production_listener_manager,
)
from app.telegram_source_gateway import TelethonTelegramSourceGateway

PRODUCTION_LISTENER_GENERATION = "canonical-v1"
PRODUCTION_AI_GENERATION = "provider-aware-v3-adaptive"
_ADAPTIVE_PROFILE_REFRESH_SECONDS = 900


class ProviderResearchProductionListener(CanonicalProductionTelegramListenerManager):
    """Lifecycle wrapper: canonical ingress plus Provider Lab learning services."""

    def __init__(
        self,
        inner: CanonicalProductionTelegramListenerManager,
        research: ProviderResearchManager,
    ) -> None:
        self._inner = inner
        self._research = research
        self._adaptive_refresh_task: asyncio.Task[None] | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def start(self) -> None:
        await self._inner.start()
        try:
            await self._research.start()
            await self._refresh_adaptive_profiles()
            self._adaptive_refresh_task = asyncio.create_task(
                self._adaptive_refresh_loop(),
                name="super-signals-adaptive-provider-profile-refresh",
            )
        except Exception:
            await self._inner.stop()
            raise

    async def stop(self) -> None:
        if self._adaptive_refresh_task is not None:
            self._adaptive_refresh_task.cancel()
            try:
                await self._adaptive_refresh_task
            except asyncio.CancelledError:
                pass
            self._adaptive_refresh_task = None
        try:
            await self._research.stop()
        finally:
            await self._inner.stop()

    async def _refresh_adaptive_profiles(self) -> None:
        pipeline = getattr(self._inner, "_ai_pipeline", None)
        refresh = getattr(pipeline, "refresh_all_provider_profiles", None)
        if callable(refresh):
            await asyncio.to_thread(refresh)

    async def _adaptive_refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(_ADAPTIVE_PROFILE_REFRESH_SECONDS)
            try:
                await self._refresh_adaptive_profiles()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Profile learning is research context. It must never stop Telegram
                # ingestion or broker routing if one refresh cycle fails.
                continue


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
        pipeline._signals = CanonicalSignalLedger(manager._session_factory)
        pipeline._lifecycle = ShadowAwareAiLifecycleBridge(manager._session_factory)
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
