"""Day 38 router whose source eligibility comes from PostgreSQL, not a duplicate env list.

The stored-decision loader in Day 28 already requires the source row to be Testing or
Live.  Production therefore does not need a second static Render UUID allow-list which
can drift from the product UI and silently suppress a newly selected provider.
"""

from __future__ import annotations

import logging
from uuid import UUID

from app.day28_full_execution import Day28RouteResult
from app.day38_full_execution import Day38FullExecutionRouter

logger = logging.getLogger(__name__)


class DatabaseSourceDay38FullExecutionRouter(Day38FullExecutionRouter):
    """Route any source that the durable DB currently marks Testing/Live."""

    def __init__(self, **kwargs):  # noqa: ANN003
        # The historical parent constructor requires a non-empty static allow-list.
        # Production dispatch below deliberately replaces that old gate with the
        # existing DB status check in _load_stored_decision.  Supply a harmless
        # constructor sentinel only to preserve backwards compatibility with the
        # parent API; it is never consulted by this subclass.
        kwargs["allowed_source_ids"] = (UUID(int=0),)
        super().__init__(**kwargs)

    async def dispatch_stored_decision(
        self,
        *,
        source_id: UUID,
        telegram_message_id: int,
        revision_index: int = 0,
    ) -> Day28RouteResult:
        stored = self._load_stored_decision(
            source_id=source_id,
            telegram_message_id=telegram_message_id,
            revision_index=revision_index,
        )
        if stored is None:
            logger.info(
                "Message ignored source=%s telegram_message_id=%s reason=%s",
                source_id,
                telegram_message_id,
                "source_not_testing_or_live_or_decision_missing",
            )
            return Day28RouteResult(
                outcome="blocked",
                decision=None,
                action=None,
                error_code="day28_stored_decision_missing",
                reason="source_not_testing_or_live_or_decision_missing",
            )

        if stored.decision == "new_trade" and stored.action == "execute":
            logger.info(
                "Dispatching new trade source=%s telegram_message_id=%s revision=%s",
                source_id,
                telegram_message_id,
                revision_index,
            )
            return await self._dispatch_new_trade(stored, revision_index)

        if stored.decision == "trade_update" and stored.action == "apply_update":
            logger.info(
                "Dispatching management update source=%s telegram_message_id=%s revision=%s",
                source_id,
                telegram_message_id,
                revision_index,
            )
            return await self._dispatch_management(stored, revision_index)

        logger.info(
            "Message ignored source=%s telegram_message_id=%s decision=%s action=%s reason=%s",
            source_id,
            telegram_message_id,
            stored.decision,
            stored.action,
            stored.reason,
        )
        return Day28RouteResult(
            outcome="ignored",
            decision=stored.decision,
            action=stored.action,
            reason=stored.reason,
        )


__all__ = ["DatabaseSourceDay38FullExecutionRouter"]
