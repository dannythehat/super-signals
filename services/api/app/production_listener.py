"""Production Telegram listener wiring.

There is one production ingress generation and one production semantic pipeline.
Production code must never select a day-numbered listener builder or install behaviour
at import time.

Safety invariant: if Telegram listening is enabled for production paper trading, the
canonical broker router must exist. A reader that records executable decisions without
a broker router is forbidden because it creates silent missed trades.
"""

from __future__ import annotations

from typing import Any

from app.canonical_signal_ledger import CanonicalSignalLedger
from app.production_ai_pipeline import ProductionAiMessagePipeline
from app.provider_research import build_provider_research_manager
from app.telegram_listener_canonical import (
    CanonicalProductionTelegramListenerManager,
    build_canonical_production_listener_manager,
)
from app.telegram_source_gateway import TelethonTelegramSourceGateway

PRODUCTION_LISTENER_GENERATION = "canonical-v1"
PRODUCTION_AI_GENERATION = "canonical-v1"


def build_production_listener_manager(**kwargs: Any) -> CanonicalProductionTelegramListenerManager:
    manager = build_canonical_production_listener_manager(**kwargs)
    if not isinstance(manager, CanonicalProductionTelegramListenerManager):
        raise RuntimeError("production_listener_generation_mismatch")
    if getattr(manager, "_canonical_router", None) is None:
        raise RuntimeError("production_execution_router_missing")

    existing_pipeline = getattr(manager, "_ai_pipeline", None)
    if existing_pipeline is not None:
        pipeline = ProductionAiMessagePipeline(
            session_factory=manager._session_factory,
            supervisor=getattr(existing_pipeline, "_supervisor", None),
        )
        pipeline._signals = CanonicalSignalLedger(manager._session_factory)
        manager._ai_pipeline = pipeline

    api_id = kwargs.get("api_id")
    api_hash = kwargs.get("api_hash")
    cipher = kwargs.get("cipher")
    session_factory = kwargs.get("session_factory")
    if api_id is not None and api_hash and cipher is not None and session_factory is not None:
        manager._provider_research_manager = build_provider_research_manager(
            session_factory=session_factory,
            cipher=cipher,
            gateway=TelethonTelegramSourceGateway(int(api_id), str(api_hash)),
        )
    return manager


__all__ = [
    "PRODUCTION_AI_GENERATION",
    "PRODUCTION_LISTENER_GENERATION",
    "build_production_listener_manager",
]
