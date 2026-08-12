"""Day 34 restart-safe Web Push delivery.

Web Push is a visibility channel only. It consumes persisted notification_events,
records one receipt per notification/device subscription, and never calls a trading or
broker mutation gateway. Push failures are isolated from signal execution.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pywebpush import WebPushException, webpush
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)
_MAX_ATTEMPTS = 5
_STALE_SENDING_SECONDS = 120


@dataclass(frozen=True, slots=True)
class PushDeliveryResult:
    seeded: int
    sent: int
    failed: int
    suppressed: int
    broker_trade_action_created: bool = False


class Day34PushNotificationManager:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        vapid_private_key: str,
        vapid_subject: str,
        poll_seconds: int = 3,
    ) -> None:
        if not vapid_private_key.strip():
            raise ValueError("day34_vapid_private_key_missing")
        if not vapid_subject.strip():
            raise ValueError("day34_vapid_subject_missing")
        if poll_seconds <= 0:
            raise ValueError("day34_push_poll_seconds_invalid")
        self._session_factory = session_factory
        self._vapid_private_key = vapid_private_key
        self._vapid_subject = vapid_subject
        self._poll_seconds = poll_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._recover_stale_sending()
        self._task = asyncio.create_task(self._run(), name="day34-web-push")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.deliver_once()
            except Exception:
                logger.exception("Day 34 push loop failed safely")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                continue

    async def deliver_once(self) -> PushDeliveryResult:
        self._recover_stale_sending()
        seeded = self._seed_deliveries()
        sent = 0
        failed = 0
        suppressed = 0
        while True:
            claimed = self._claim_one()
            if claimed is None:
                break
            outcome = await asyncio.to_thread(self._send_claimed, claimed)
            if outcome == "sent":
                sent += 1
            elif outcome == "suppressed":
                suppressed += 1
            else:
                failed += 1
        return PushDeliveryResult(
            seeded=seeded,
            sent=sent,
            failed=failed,
            suppressed=suppressed,
        )

    def _recover_stale_sending(self) -> int:
        """Recover a worker crash without inventing a new logical notification.

        A process can die after claiming a row. The unique notification/subscription
        key ensures there is still only one logical delivery row. Retrying a stale claim
        may cause a network-level duplicate if the push service accepted the first send
        immediately before the crash, so the PWA service worker uses notification_id as
        a stable display tag and replaces the same visible notification.
        """
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    UPDATE push_notification_deliveries
                    SET status='failed',
                        failure_code='push_delivery_recovered_after_restart',
                        failure_reason='Recovered stale sending state after worker restart.',
                        updated_at=now()
                    WHERE status='sending'
                      AND sent_at IS NULL
                      AND attempted_at IS NOT NULL
                      AND attempted_at < now() - make_interval(secs => :stale_seconds)
                    RETURNING id
                    """
                ),
                {"stale_seconds": _STALE_SENDING_SECONDS},
            ).all()
            session.commit()
            return len(rows)

    def _seed_deliveries(self) -> int:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    INSERT INTO push_notification_deliveries (
                        notification_id, subscription_id, status
                    )
                    SELECT n.id, ps.id, 'pending'
                    FROM notification_events AS n
                    JOIN push_subscriptions AS ps
                      ON ps.enabled = true
                     AND n.created_at >= ps.active_since
                     AND (
                         n.audience = 'shared'
                         OR (n.audience = 'user' AND n.user_id = ps.user_id)
                     )
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM push_notification_deliveries AS d
                        WHERE d.notification_id = n.id
                          AND d.subscription_id = ps.id
                    )
                    ON CONFLICT (notification_id, subscription_id) DO NOTHING
                    RETURNING id
                    """
                )
            ).all()
            session.commit()
            return len(rows)

    def _claim_one(self) -> dict[str, Any] | None:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    WITH candidate AS (
                        SELECT d.id
                        FROM push_notification_deliveries AS d
                        JOIN push_subscriptions AS ps ON ps.id = d.subscription_id
                        WHERE d.status IN ('pending','failed')
                          AND d.attempt_count < :max_attempts
                          AND ps.enabled = true
                        ORDER BY d.created_at, d.id
                        FOR UPDATE OF d SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE push_notification_deliveries AS d
                    SET status='sending',
                        attempt_count=d.attempt_count+1,
                        attempted_at=now(),
                        failure_code=NULL,
                        failure_reason=NULL,
                        updated_at=now()
                    FROM candidate AS c
                    WHERE d.id = c.id
                    RETURNING d.id
                    """
                ),
                {"max_attempts": _MAX_ATTEMPTS},
            ).mappings().first()
            if row is None:
                session.commit()
                return None

            payload = session.execute(
                text(
                    """
                    SELECT
                        d.id AS delivery_id,
                        d.subscription_id,
                        n.id AS notification_id,
                        n.kind,
                        n.title,
                        n.body,
                        ps.endpoint,
                        ps.p256dh,
                        ps.auth
                    FROM push_notification_deliveries AS d
                    JOIN notification_events AS n ON n.id = d.notification_id
                    JOIN push_subscriptions AS ps ON ps.id = d.subscription_id
                    WHERE d.id = :delivery_id
                    """
                ),
                {"delivery_id": row["id"]},
            ).mappings().one()
            session.commit()
            return dict(payload)

    def _send_claimed(self, row: dict[str, Any]) -> str:
        delivery_id = UUID(str(row["delivery_id"]))
        subscription_id = UUID(str(row["subscription_id"]))
        data = json.dumps(
            {
                "notification_id": str(row["notification_id"]),
                "kind": str(row["kind"]),
                "title": str(row["title"]),
                "body": str(row["body"]),
                "url": "/",
                "private_account_data_included": False,
            },
            separators=(",", ":"),
        )
        subscription = {
            "endpoint": str(row["endpoint"]),
            "keys": {
                "p256dh": str(row["p256dh"]),
                "auth": str(row["auth"]),
            },
        }
        try:
            webpush(
                subscription_info=subscription,
                data=data,
                vapid_private_key=self._vapid_private_key,
                vapid_claims={"sub": self._vapid_subject},
                ttl=300,
                timeout=10,
            )
        except WebPushException as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code in {404, 410}:
                self._suppress_expired(delivery_id, subscription_id, status_code)
                return "suppressed"
            self._record_failure(
                delivery_id,
                subscription_id,
                code=f"web_push_{status_code or 'error'}",
                reason=str(exc)[:500],
            )
            return "failed"
        except Exception as exc:
            self._record_failure(
                delivery_id,
                subscription_id,
                code="web_push_unexpected",
                reason=str(exc)[:500],
            )
            return "failed"

        self._record_success(delivery_id, subscription_id)
        return "sent"

    def _record_success(self, delivery_id: UUID, subscription_id: UUID) -> None:
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE push_notification_deliveries
                    SET status='sent', sent_at=now(), failure_code=NULL,
                        failure_reason=NULL, updated_at=now()
                    WHERE id=:delivery_id
                    """
                ),
                {"delivery_id": delivery_id},
            )
            session.execute(
                text(
                    """
                    UPDATE push_subscriptions
                    SET failure_count=0, last_success_at=now(), updated_at=now()
                    WHERE id=:subscription_id
                    """
                ),
                {"subscription_id": subscription_id},
            )
            session.commit()

    def _record_failure(
        self,
        delivery_id: UUID,
        subscription_id: UUID,
        *,
        code: str,
        reason: str,
    ) -> None:
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE push_notification_deliveries
                    SET status='failed', failure_code=:code, failure_reason=:reason,
                        updated_at=now()
                    WHERE id=:delivery_id
                    """
                ),
                {"delivery_id": delivery_id, "code": code[:80], "reason": reason[:500]},
            )
            session.execute(
                text(
                    """
                    UPDATE push_subscriptions
                    SET failure_count=failure_count+1, last_failure_at=now(), updated_at=now()
                    WHERE id=:subscription_id
                    """
                ),
                {"subscription_id": subscription_id},
            )
            session.commit()

    def _suppress_expired(
        self,
        delivery_id: UUID,
        subscription_id: UUID,
        status_code: int,
    ) -> None:
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE push_notification_deliveries
                    SET status='suppressed', failure_code=:code,
                        failure_reason='Browser push subscription expired or was removed.',
                        updated_at=now()
                    WHERE id=:delivery_id
                    """
                ),
                {"delivery_id": delivery_id, "code": f"web_push_{status_code}"},
            )
            session.execute(
                text(
                    """
                    UPDATE push_subscriptions
                    SET enabled=false, failure_count=failure_count+1,
                        last_failure_at=now(), updated_at=now()
                    WHERE id=:subscription_id
                    """
                ),
                {"subscription_id": subscription_id},
            )
            session.commit()


__all__ = ["Day34PushNotificationManager", "PushDeliveryResult"]
