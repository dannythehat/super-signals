"""Trade-capture reliability fixes derived from the complete 18 Aug paper audit.

Two listener gaps mattered for trade capture:

1. The 15-second live-recovery sweep only broker-dispatched an original/edit when that
   sweep had just inserted the database row. If the live push path persisted and AI-
   supervised a fresh signal but failed between that point and broker dispatch, later
   recovery saw an existing row and never repaired the missing route.
2. Recovered ``trade_update/apply_update`` decisions were dispatched repeatedly even
   when the AI lifecycle bridge had no durable lifecycle event for that revision. The
   same 48 messages generated 1,231 route failures on 18 Aug without any broker action.

Recovery now treats PostgreSQL as a durable hand-off: every still-fresh actionable
message/revision is reconsidered by the idempotent router even when already persisted.
Successful or previously-blocked new-trade routes remain protected by the router's
existing audit/idempotency gate. Recovered management is routed only when its durable
lifecycle event exists. Stale new trades remain evidence only.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

_installed = False
logger = logging.getLogger(__name__)


def install_aug18_trade_capture_overrides() -> None:
    global _installed
    if _installed:
        return

    from app.telegram_listener import CapturedTelegramMessage, ReaderListeningPlan
    from app.telegram_listener_day13 import CapturedTelegramEdit
    from app.telegram_listener_day21 import Day21TelegramListenerManager
    from app.telegram_listener_day38 import PaperPendingAwareListenerManager

    original_dispatch_recovered = PaperPendingAwareListenerManager._dispatch_recovered_if_required

    if not getattr(original_dispatch_recovered, "_unresolved_management_filtered", False):

        async def dispatch_recovered_if_required(
            self: Any,
            *,
            source_id,
            telegram_message_id: int,
            revision_index: int,
            occurred_at,
        ) -> None:
            router = self._day28_router
            if router is None:
                return

            stored = await asyncio.to_thread(
                router._load_stored_decision,
                source_id=source_id,
                telegram_message_id=telegram_message_id,
                revision_index=revision_index,
            )
            if stored is None:
                return

            if stored.decision == "trade_update" and stored.action == "apply_update":
                lifecycle_event_id, signal_id = await asyncio.to_thread(
                    router._resolve_lifecycle_event,
                    stored.message_id,
                    revision_index,
                )
                if lifecycle_event_id is None or signal_id is None:
                    # The semantic decision is retained as evidence. Re-running the
                    # same unresolved management revision every recovery sweep cannot
                    # create a valid broker target and only floods the audit trail.
                    logger.info(
                        "Recovered management retained as evidence: lifecycle target unresolved",
                        extra={
                            "source_id": str(source_id),
                            "telegram_message_id": telegram_message_id,
                            "revision_index": revision_index,
                        },
                    )
                    return

            await original_dispatch_recovered(
                self,
                source_id=source_id,
                telegram_message_id=telegram_message_id,
                revision_index=revision_index,
                occurred_at=occurred_at,
            )

        dispatch_recovered_if_required._unresolved_management_filtered = True  # type: ignore[attr-defined]
        PaperPendingAwareListenerManager._dispatch_recovered_if_required = dispatch_recovered_if_required

    original_recover = PaperPendingAwareListenerManager._recover_live_gaps
    if not getattr(original_recover, "_durable_dispatch_self_heal", False):

        async def recover_live_gaps(
            self: Any,
            client: Any,
            plan: ReaderListeningPlan,
        ) -> None:
            """Repair fresh persisted-but-unrouted signals as well as missed inserts."""
            for source in plan.sources:
                try:
                    messages = await client.get_messages(
                        source.chat_id,
                        limit=50,
                    )
                except Exception:
                    logger.exception(
                        "Telegram live recovery skipped one unreadable source",
                        extra={
                            "source_id": str(source.source_id),
                            "chat_id": source.chat_id,
                        },
                    )
                    continue

                for message in reversed(list(messages)):
                    message_id = getattr(message, "id", None)
                    if message_id is None:
                        continue
                    reply_to = getattr(message, "reply_to", None)
                    reply_to_message_id = getattr(reply_to, "reply_to_msg_id", None)
                    media = getattr(message, "media", None)
                    raw_text = str(getattr(message, "raw_text", "") or "")
                    posted_at = self._utc_datetime(getattr(message, "date", None))
                    captured = CapturedTelegramMessage(
                        source_id=source.source_id,
                        chat_id=source.chat_id,
                        telegram_message_id=int(message_id),
                        raw_text=raw_text,
                        posted_at=posted_at,
                        reply_to_message_id=(
                            int(reply_to_message_id)
                            if reply_to_message_id is not None
                            else None
                        ),
                        has_media=media is not None,
                        media_type=type(media).__name__ if media is not None else None,
                    )

                    # Use Day21 directly so persistence + AI supervision occur without
                    # the Day28 immediate-dispatch wrapper. Then always consult the
                    # durable decision below, even when this row already existed.
                    await asyncio.to_thread(
                        Day21TelegramListenerManager._persist_message,
                        self,
                        captured,
                    )
                    await self._dispatch_recovered_if_required(
                        source_id=source.source_id,
                        telegram_message_id=int(message_id),
                        revision_index=0,
                        occurred_at=posted_at,
                    )

                    edit_date = getattr(message, "edit_date", None)
                    if edit_date is None:
                        continue
                    edited_at = self._utc_datetime(edit_date)
                    captured_edit = CapturedTelegramEdit(
                        source_id=source.source_id,
                        chat_id=source.chat_id,
                        telegram_message_id=int(message_id),
                        raw_text=raw_text,
                        edited_at=edited_at,
                        reply_to_message_id=(
                            int(reply_to_message_id)
                            if reply_to_message_id is not None
                            else None
                        ),
                        has_media=media is not None,
                        media_type=type(media).__name__ if media is not None else None,
                    )
                    await asyncio.to_thread(
                        Day21TelegramListenerManager._persist_edit,
                        self,
                        captured_edit,
                    )
                    revision_index = await asyncio.to_thread(
                        self._latest_revision_index,
                        source.source_id,
                        int(message_id),
                    )
                    if revision_index > 0:
                        # Dispatch the durable latest revision even when another live
                        # handler already stored it. This repairs the narrow crash/race
                        # window between AI completion and broker routing.
                        await self._dispatch_recovered_if_required(
                            source_id=source.source_id,
                            telegram_message_id=int(message_id),
                            revision_index=revision_index,
                            occurred_at=edited_at,
                        )

        recover_live_gaps._durable_dispatch_self_heal = True  # type: ignore[attr-defined]
        PaperPendingAwareListenerManager._recover_live_gaps = recover_live_gaps

    _installed = True


__all__ = ["install_aug18_trade_capture_overrides"]
