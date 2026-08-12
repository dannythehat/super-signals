"""One-shot, user-only Day 34 Web Push acceptance helper.

This helper never creates a trade, Signal, lifecycle event or Telegram publication. It
only creates one private notification_events row after the configured owner has an
active push subscription. The normal Day34PushNotificationManager must deliver it.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)
_EVENT_KEY = "day34-push-acceptance-v1"


async def run_day34_push_acceptance(
    *,
    session_factory: sessionmaker[Session],
    user_id: UUID,
    timeout_seconds: int = 20,
) -> None:
    """Create one private test notification only when a device is already opted in."""
    deadline = asyncio.get_running_loop().time() + max(1, timeout_seconds)
    while asyncio.get_running_loop().time() < deadline:
        with session_factory() as session:
            subscription = session.execute(
                text(
                    """
                    SELECT id, active_since
                    FROM push_subscriptions
                    WHERE user_id=:user_id AND enabled=true
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """
                ),
                {"user_id": user_id},
            ).mappings().first()
            if subscription is not None:
                existing = session.execute(
                    text("SELECT id FROM notification_events WHERE event_key=:event_key"),
                    {"event_key": _EVENT_KEY},
                ).scalar_one_or_none()
                if existing is None:
                    notification_id = session.execute(
                        text(
                            """
                            INSERT INTO notification_events (
                                event_key, user_id, audience, kind, title, body, payload
                            ) VALUES (
                                :event_key, :user_id, 'user', 'system_test',
                                'Super Signals alerts are working',
                                'Trade alerts are enabled on this device.',
                                jsonb_build_object(
                                    'day34_push_acceptance', true,
                                    'private_account_data_included', false,
                                    'trade_action_created', false
                                )
                            )
                            RETURNING id
                            """
                        ),
                        {"event_key": _EVENT_KEY, "user_id": user_id},
                    ).scalar_one()
                    session.commit()
                    logger.info(
                        "Day 34 push acceptance notification created id=%s subscription=%s",
                        notification_id,
                        subscription["id"],
                    )
                else:
                    logger.info("Day 34 push acceptance notification already exists id=%s", existing)
                return
        await asyncio.sleep(1)
    logger.info("Day 34 push acceptance skipped: no enabled device subscription")


__all__ = ["run_day34_push_acceptance"]
