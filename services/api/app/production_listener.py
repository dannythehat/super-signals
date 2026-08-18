"""Canonical production Telegram listener wiring.

Production code must import this module, never a day-numbered listener builder directly.
Day-numbered modules remain implementation history/dependencies only.

Safety invariant: if Telegram listening is enabled for production paper trading, the
Day 38 database-driven broker router must exist. Running a reader that accepts signals
without a broker router is forbidden because it creates the exact silent failure mode
where valid signals are stored as executable but no positions are placed.
"""

from __future__ import annotations

import os
from typing import Any

from app.telegram_listener_day38 import (
    PaperPendingAwareListenerManager,
    build_day38_listener_manager,
)

PRODUCTION_LISTENER_GENERATION = "day38"


def build_production_listener_manager(**kwargs: Any) -> PaperPendingAwareListenerManager:
    manager = build_day38_listener_manager(**kwargs)
    if not isinstance(manager, PaperPendingAwareListenerManager):
        raise RuntimeError("production_listener_generation_mismatch")

    # A production reader without an execution router is worse than a hard failure:
    # it records 'execute/accepted' decisions while silently placing zero trades.
    # Do not allow that state to boot.
    if getattr(manager, "_day28_router", None) is None:
        raise RuntimeError("production_execution_router_missing")

    return manager


__all__ = [
    "PRODUCTION_LISTENER_GENERATION",
    "build_production_listener_manager",
]
