"""Restart-safe Web Push delivery for current Super Signals events only.

Web Push is a visibility channel only. It consumes persisted notification events,
records one receipt per notification/device subscription, and never calls a trading or
broker mutation gateway.

Historical lifecycle rows are audit evidence, not a restart replay queue. A lifecycle
notification is push-eligible only when it was materialised within five minutes of its
underlying lifecycle event. Existing stale pending/failed push deliveries are suppressed
before they can ever be claimed after a restart.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from uuid import UUID

from pywebpush import WebPushException, webpush
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)
_MAX_ATTEMPTS = 5
_STALE_SENDING_SECONDS = 120
_PUSH_TTL_SECONDS = 24 * 60 * 60
_PUSH_HEADERS = {"Urgency": "high"}


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
        self._task = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._recover_stale_sending()
        self._suppress_stale_lifecycle_deliveries()
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
                pass

    async def deliver_once(self) -> PushDeliveryResult:
        self._recover_stale_sending()
        suppressed = self._suppress_stale_lifecycle_deliveries()
        seeded = self._seed_deliveries()
        sent = failed = 0
        while True:
            row = self._claim_one()
            if row is None:
                break
            outcome = await asyncio.to_thread(self._send_claimed, row)
            if outcome == "sent":
                sent += 1
            elif outcome == "suppressed":
                suppressed += 1
            else:
                failed += 1
        return PushDeliveryResult(seeded, sent, failed, suppressed)

    def _recover_stale_sending(self) -> int:
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
                      AND attempted_at < now()-make_interval(secs=>:seconds)
                    RETURNING id
                    """
                ),
                {"seconds": _STALE_SENDING_SECONDS},
            ).all()
            session.commit()
            return len(rows)

    def _suppress_stale_lifecycle_deliveries(self) -> int:
        """Retire queued historical lifecycle pushes while retaining audit records."""
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    UPDATE push_notification_deliveries AS delivery
                    SET status='suppressed',
                        failure_code='stale_lifecycle_audit_only',
                        failure_reason='Historical lifecycle notifications are audit-only and are never replayed to devices.',
                        updated_at=now()
                    FROM notification_events AS notification
                    JOIN signal_lifecycle_events AS lifecycle
                      ON lifecycle.id=notification.lifecycle_event_id
                    WHERE delivery.notification_id=notification.id
                      AND delivery.status IN ('pending','failed')
                      AND notification.created_at > lifecycle.occurred_at + interval '5 minutes'
                    RETURNING delivery.id
                    """
                )
            ).all()
            session.commit()
            return len(rows)

    def _seed_deliveries(self) -> int:
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    INSERT INTO notification_events(
                        event_key,signal_id,lifecycle_event_id,user_id,
                        audience,kind,title,body,payload
                    )
                    SELECT
                        'push-enabled:'||subscription.id::text||':'||
                            to_char(subscription.active_since AT TIME ZONE 'UTC','YYYYMMDDHH24MISSUS'),
                        NULL,NULL,subscription.user_id,'user','push_test',
                        'Super Signals alerts are on',
                        'This device is ready to receive live trade alerts.',
                        jsonb_build_object(
                            'subscription_confirmation',true,
                            'provider_identity_exposed',false,
                            'private_balance_exposed',false,
                            'trade_action_created',false
                        )
                    FROM push_subscriptions AS subscription
                    WHERE subscription.enabled=true
                      AND NOT EXISTS(
                          SELECT 1
                          FROM notification_events AS existing
                          WHERE existing.event_key='push-enabled:'||subscription.id::text||':'||
                              to_char(subscription.active_since AT TIME ZONE 'UTC','YYYYMMDDHH24MISSUS')
                      )
                    ON CONFLICT(event_key) DO NOTHING
                    """
                )
            )
            rows = session.execute(
                text(
                    """
                    INSERT INTO push_notification_deliveries(
                        notification_id,subscription_id,status
                    )
                    SELECT notification.id,subscription.id,'pending'
                    FROM notification_events AS notification
                    JOIN push_subscriptions AS subscription
                      ON subscription.enabled=true
                     AND notification.created_at>=subscription.active_since
                     AND (
                         notification.audience='shared'
                         OR (
                             notification.audience='user'
                             AND notification.user_id=subscription.user_id
                         )
                     )
                    WHERE (
                        notification.lifecycle_event_id IS NULL
                        OR EXISTS (
                            SELECT 1
                            FROM signal_lifecycle_events AS lifecycle
                            WHERE lifecycle.id=notification.lifecycle_event_id
                              AND notification.created_at <= lifecycle.occurred_at + interval '5 minutes'
                        )
                    )
                      AND NOT EXISTS(
                          SELECT 1
                          FROM push_notification_deliveries AS existing
                          WHERE existing.notification_id=notification.id
                            AND existing.subscription_id=subscription.id
                      )
                    ON CONFLICT(notification_id,subscription_id) DO NOTHING
                    RETURNING id
                    """
                )
            ).all()
            session.commit()
            return len(rows)

    def _claim_one(self):
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    WITH candidate AS (
                        SELECT delivery.id
                        FROM push_notification_deliveries AS delivery
                        JOIN push_subscriptions AS subscription
                          ON subscription.id=delivery.subscription_id
                        JOIN notification_events AS notification
                          ON notification.id=delivery.notification_id
                        WHERE delivery.status IN ('pending','failed')
                          AND delivery.attempt_count<:max_attempts
                          AND subscription.enabled=true
                          AND (
                              notification.lifecycle_event_id IS NULL
                              OR EXISTS (
                                  SELECT 1
                                  FROM signal_lifecycle_events AS lifecycle
                                  WHERE lifecycle.id=notification.lifecycle_event_id
                                    AND notification.created_at <= lifecycle.occurred_at + interval '5 minutes'
                              )
                          )
                        ORDER BY delivery.created_at,delivery.id
                        FOR UPDATE OF delivery SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE push_notification_deliveries AS delivery
                    SET status='sending',
                        attempt_count=delivery.attempt_count+1,
                        attempted_at=now(),
                        failure_code=NULL,
                        failure_reason=NULL,
                        updated_at=now()
                    FROM candidate
                    WHERE delivery.id=candidate.id
                    RETURNING delivery.id
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
                        delivery.id AS delivery_id,
                        delivery.subscription_id,
                        notification.id AS notification_id,
                        notification.kind,
                        notification.title,
                        notification.body,
                        subscription.endpoint,
                        subscription.p256dh,
                        subscription.auth
                    FROM push_notification_deliveries AS delivery
                    JOIN notification_events AS notification
                      ON notification.id=delivery.notification_id
                    JOIN push_subscriptions AS subscription
                      ON subscription.id=delivery.subscription_id
                    WHERE delivery.id=:delivery_id
                    """
                ),
                {"delivery_id": row["id"]},
            ).mappings().one()
            session.commit()
            return dict(payload)

    def _send_claimed(self, row) -> str:
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
                ttl=_PUSH_TTL_SECONDS,
                headers=_PUSH_HEADERS,
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
                    SET status='sent',sent_at=now(),failure_code=NULL,
                        failure_reason=NULL,updated_at=now()
                    WHERE id=:delivery_id
                    """
                ),
                {"delivery_id": delivery_id},
            )
            session.execute(
                text(
                    """
                    UPDATE push_subscriptions
                    SET failure_count=0,last_success_at=now(),updated_at=now()
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
                    SET status='failed',failure_code=:code,failure_reason=:reason,updated_at=now()
                    WHERE id=:delivery_id
                    """
                ),
                {"delivery_id": delivery_id, "code": code[:80], "reason": reason[:500]},
            )
            session.execute(
                text(
                    """
                    UPDATE push_subscriptions
                    SET failure_count=failure_count+1,last_failure_at=now(),updated_at=now()
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
                    SET status='suppressed',failure_code=:code,
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
                    SET enabled=false,failure_count=failure_count+1,last_failure_at=now(),updated_at=now()
                    WHERE id=:subscription_id
                    """
                ),
                {"subscription_id": subscription_id},
            )
            session.commit()


__all__ = ["Day34PushNotificationManager", "PushDeliveryResult"]
