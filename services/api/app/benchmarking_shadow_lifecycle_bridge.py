"""Provider Lab benchmark mirror for testing/live lifecycle management."""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import text

from app.shadow_lifecycle_bridge import ShadowAwareAiLifecycleBridge
from app.shadow_trading import ShadowTradeService

logger = logging.getLogger(__name__)


class BenchmarkingShadowAwareAiLifecycleBridge(ShadowAwareAiLifecycleBridge):
    """Mirror testing/live provider management into research without blocking broker flow."""

    def __init__(self, session_factory) -> None:
        super().__init__(session_factory)
        self._benchmark_shadow = ShadowTradeService(session_factory)

    def process(
        self,
        *,
        message_id: UUID,
        extracted: dict,
        revision_index: int = 0,
    ):
        result = super().process(
            message_id=message_id,
            extracted=extracted,
            revision_index=revision_index,
        )
        if result.linked and result.event_id is not None:
            self._mirror_testing_live_management(message_id, result.event_id)
        return result

    def _mirror_testing_live_management(self, message_id: UUID, event_id: UUID) -> None:
        try:
            with self._session_factory() as session:
                status = session.execute(
                    text(
                        """
                        SELECT s.status FROM messages m
                        JOIN sources s ON s.id=m.source_id
                        WHERE m.id=:message_id LIMIT 1
                        """
                    ),
                    {"message_id": message_id},
                ).scalar_one_or_none()
            if str(status or "") not in {"testing", "live"}:
                return
            self._benchmark_shadow.record_management(event_id)
        except Exception:
            logger.exception(
                "Provider Lab management mirror failed safely",
                extra={"message_id": str(message_id), "event_id": str(event_id)},
            )


__all__ = ["BenchmarkingShadowAwareAiLifecycleBridge"]
