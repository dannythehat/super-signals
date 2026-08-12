"""Day 34 cutover-safe Telegram publication seeding.

This thin runtime layer keeps the full Day34 publisher implementation intact while
preventing deployment from replaying historical Day26-33 Signals, lifecycle events or
in-app alerts as if they were new. The pinned Live Trades Board is intentionally NOT
cutover-filtered: anything still broker-active at deployment belongs on the live board.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from app.telegram_publisher_day34 import Day34TelegramPublisherManager


class Day34CutoverTelegramPublisherManager(Day34TelegramPublisherManager):
    """Day34 publisher with a persisted forward-only member-notification boundary."""

    def _seed_missing_publications(self) -> None:
        with self._session_factory() as session:
            publish_after = session.execute(
                text("SELECT publish_after FROM day34_summary_state WHERE id=1")
            ).scalar_one()

            # A canonical Signal can appear before Day26 finishes broker placement. It
            # is suppressed from the member feed until the success audit exists. Old
            # pre-cutover placement successes do not become new Day34 root posts.
            session.execute(
                text(
                    """
                    UPDATE telegram_publications AS pub
                    SET status='suppressed',
                        failure_code='day34_not_broker_placed',
                        failure_reason='Day 34 publishes roots only after confirmed broker placement.',
                        updated_at=now()
                    WHERE pub.publication_kind='signal_created'
                      AND pub.lifecycle_event_id IS NULL
                      AND pub.status='pending'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM audit_events AS placed
                          WHERE placed.entity_type='signal'
                            AND placed.entity_id=pub.signal_id
                            AND placed.event_type='mt5.day26_execution_success'
                            AND placed.created_at > :publish_after
                      )
                    """
                ),
                {"publish_after": publish_after},
            )

            # If the publisher polled between Signal creation and broker completion,
            # reactivate ONLY the row we suppressed for this Day34 reason and ONLY when
            # a post-cutover confirmed placement subsequently appears.
            session.execute(
                text(
                    """
                    UPDATE telegram_publications AS pub
                    SET status='pending',
                        failure_code=NULL,
                        failure_reason=NULL,
                        updated_at=now()
                    WHERE pub.publication_kind='signal_created'
                      AND pub.lifecycle_event_id IS NULL
                      AND pub.status='suppressed'
                      AND pub.failure_code='day34_not_broker_placed'
                      AND pub.telegram_message_id IS NULL
                      AND EXISTS (
                          SELECT 1
                          FROM audit_events AS placed
                          WHERE placed.entity_type='signal'
                            AND placed.entity_id=pub.signal_id
                            AND placed.event_type='mt5.day26_execution_success'
                            AND placed.created_at > :publish_after
                      )
                    """
                ),
                {"publish_after": publish_after},
            )

            session.execute(
                text(
                    """
                    INSERT INTO telegram_publications (
                        signal_id, publication_kind, status
                    )
                    SELECT sig.id, 'signal_created', 'pending'
                    FROM signals AS sig
                    WHERE EXISTS (
                        SELECT 1
                        FROM audit_events AS placed
                        WHERE placed.entity_type='signal'
                          AND placed.entity_id=sig.id
                          AND placed.event_type='mt5.day26_execution_success'
                          AND placed.created_at > :publish_after
                    )
                      AND NOT EXISTS (
                        SELECT 1
                        FROM telegram_publications AS pub
                        WHERE pub.signal_id=sig.id
                          AND pub.publication_kind='signal_created'
                          AND pub.lifecycle_event_id IS NULL
                      )
                    ON CONFLICT DO NOTHING
                    """
                ),
                {"publish_after": publish_after},
            )

            # Lifecycle events are forward-only as well. An older active trade may
            # still receive new management/settlement events after cutover, but those
            # can publish only if a historical root was already sent or this trade was
            # placed after cutover and therefore has a Day34 root path.
            session.execute(
                text(
                    """
                    INSERT INTO telegram_publications (
                        signal_id, lifecycle_event_id, publication_kind, status
                    )
                    SELECT ev.signal_id, ev.id, 'lifecycle_event', 'pending'
                    FROM signal_lifecycle_events AS ev
                    WHERE ev.occurred_at > :publish_after
                      AND (
                          EXISTS (
                              SELECT 1
                              FROM telegram_publications AS root
                              WHERE root.signal_id=ev.signal_id
                                AND root.publication_kind='signal_created'
                                AND root.lifecycle_event_id IS NULL
                                AND root.status='sent'
                                AND root.telegram_message_id IS NOT NULL
                          )
                          OR EXISTS (
                              SELECT 1
                              FROM audit_events AS placed
                              WHERE placed.entity_type='signal'
                                AND placed.entity_id=ev.signal_id
                                AND placed.event_type='mt5.day26_execution_success'
                                AND placed.created_at > :publish_after
                          )
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM telegram_publications AS pub
                          WHERE pub.lifecycle_event_id=ev.id
                      )
                    ON CONFLICT DO NOTHING
                    """
                ),
                {"publish_after": publish_after},
            )

            self._seed_in_app_notifications_after_cutover(
                session,
                publish_after=publish_after,
            )
            session.commit()

        if self._summary_service is not None:
            self._summary_service.seed_due()
        self._seed_summary_deliveries()

        # Intentionally sees ALL current broker-active state, including a trade opened
        # before deployment that is still live after deployment.
        self._sync_live_board_safely()

    @staticmethod
    def _seed_in_app_notifications_after_cutover(
        session: Any,
        *,
        publish_after: Any,
    ) -> None:
        session.execute(
            text(
                """
                INSERT INTO notification_events (
                    event_key, signal_id, lifecycle_event_id, user_id,
                    audience, kind, title, body, payload
                )
                SELECT
                    'signal-open:' || sig.id::text,
                    sig.id,
                    NULL,
                    NULL,
                    'shared',
                    'trade_open',
                    COALESCE(sig.symbol,'') || ' ' || COALESCE(sig.side,'') || ' opened',
                    'Trade placed and confirmed at the broker.',
                    jsonb_build_object(
                        'broker_confirmed', true,
                        'provider_identity_exposed', false,
                        'trade_action_created', false
                    )
                FROM signals AS sig
                WHERE EXISTS (
                    SELECT 1
                    FROM audit_events AS placed
                    WHERE placed.entity_type='signal'
                      AND placed.entity_id=sig.id
                      AND placed.event_type='mt5.day26_execution_success'
                      AND placed.created_at > :publish_after
                )
                ON CONFLICT (event_key) DO NOTHING
                """
            ),
            {"publish_after": publish_after},
        )

        session.execute(
            text(
                """
                INSERT INTO notification_events (
                    event_key, signal_id, lifecycle_event_id, user_id,
                    audience, kind, title, body, payload
                )
                SELECT
                    'lifecycle:' || ev.id::text,
                    ev.signal_id,
                    ev.id,
                    NULL,
                    'shared',
                    CASE
                        WHEN ev.event_type LIKE 'broker_result_%' THEN 'trade_result'
                        ELSE 'trade_update'
                    END,
                    CASE
                        WHEN ev.event_type='broker_result_win' THEN 'Trade won'
                        WHEN ev.event_type='broker_result_loss' THEN 'Trade lost'
                        WHEN ev.event_type='broker_result_breakeven' THEN 'Trade closed at break even'
                        WHEN ev.event_type='broker_result_closed' THEN 'Trade closed'
                        ELSE 'Trade update'
                    END,
                    ev.rendered_text,
                    jsonb_build_object(
                        'origin', ev.origin,
                        'broker_result', ev.event_type LIKE 'broker_result_%',
                        'provider_identity_exposed', false,
                        'trade_action_created', false
                    )
                FROM signal_lifecycle_events AS ev
                WHERE ev.occurred_at > :publish_after
                  AND (
                      EXISTS (
                          SELECT 1
                          FROM telegram_publications AS root
                          WHERE root.signal_id=ev.signal_id
                            AND root.publication_kind='signal_created'
                            AND root.lifecycle_event_id IS NULL
                            AND root.status='sent'
                            AND root.telegram_message_id IS NOT NULL
                      )
                      OR EXISTS (
                          SELECT 1
                          FROM audit_events AS placed
                          WHERE placed.entity_type='signal'
                            AND placed.entity_id=ev.signal_id
                            AND placed.event_type='mt5.day26_execution_success'
                            AND placed.created_at > :publish_after
                      )
                  )
                ON CONFLICT (event_key) DO NOTHING
                """
            ),
            {"publish_after": publish_after},
        )


__all__ = ["Day34CutoverTelegramPublisherManager"]
