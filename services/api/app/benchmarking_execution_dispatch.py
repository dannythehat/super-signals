"""Canonical execution dispatcher with a non-blocking Provider Lab benchmark sidecar.

Testing/LIVE provider decisions still follow the normal broker/member route. In parallel,
the same provider instruction is enrolled in the fixed-dollar shadow benchmark so live
providers accumulate comparable research evidence without changing broker behaviour.
"""

from __future__ import annotations

import logging
from uuid import UUID

from app.execution_dispatch_canonical import CanonicalExecutionDispatcher, CanonicalRouteResult

logger = logging.getLogger(__name__)


class BenchmarkingCanonicalExecutionDispatcher(CanonicalExecutionDispatcher):
    """Mirror testing/LIVE canonical instructions into isolated Provider Lab state."""

    async def dispatch_stored_decision(
        self,
        *,
        source_id: UUID,
        telegram_message_id: int,
        revision_index: int = 0,
    ) -> CanonicalRouteResult:
        stored = self._load_stored_decision(
            source_id=source_id,
            telegram_message_id=telegram_message_id,
            revision_index=revision_index,
        )
        if stored is not None and stored.source_status in {"testing", "live"}:
            self._record_benchmark_sidecar(stored, revision_index)
        return await super().dispatch_stored_decision(
            source_id=source_id,
            telegram_message_id=telegram_message_id,
            revision_index=revision_index,
        )

    def _record_benchmark_sidecar(self, stored, revision_index: int) -> None:
        """Research failure must never block or alter canonical broker routing."""
        try:
            if stored.decision == "new_trade" and stored.action == "execute":
                signal_id = self._resolve_signal_id(stored.message_id, revision_index)
                if signal_id is not None:
                    self._shadow.record_signal(signal_id)
                return
            if stored.decision == "trade_update" and stored.action == "apply_update":
                event_id, _signal_id = self._resolve_lifecycle_event(
                    stored.message_id,
                    revision_index,
                )
                if event_id is not None:
                    self._shadow.record_management(event_id)
        except Exception:
            logger.exception(
                "Provider Lab sidecar failed without blocking broker route",
                extra={"message_id": str(stored.message_id), "revision_index": revision_index},
            )


__all__ = ["BenchmarkingCanonicalExecutionDispatcher"]
