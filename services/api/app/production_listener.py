"""Production Telegram listener wiring.

There is one production ingress generation: the canonical listener.  Production code
must never select a day-numbered listener builder or install behaviour at import time.

Safety invariant: if Telegram listening is enabled for production paper trading, the
database-driven broker router must exist.  A reader that records executable decisions
without a broker router is forbidden because it creates silent missed trades.
"""

from __future__ import annotations

from typing import Any

from app.telegram_listener_canonical import (
    CanonicalProductionTelegramListenerManager,
    build_canonical_production_listener_manager,
)

PRODUCTION_LISTENER_GENERATION = "canonical-v1"


def build_production_listener_manager(**kwargs: Any) -> CanonicalProductionTelegramListenerManager:
    manager = build_canonical_production_listener_manager(**kwargs)
    if not isinstance(manager, CanonicalProductionTelegramListenerManager):
        raise RuntimeError("production_listener_generation_mismatch")
    if getattr(manager, "_day28_router", None) is None:
        raise RuntimeError("production_execution_router_missing")
    return manager


__all__ = [
    "PRODUCTION_LISTENER_GENERATION",
    "build_production_listener_manager",
]
