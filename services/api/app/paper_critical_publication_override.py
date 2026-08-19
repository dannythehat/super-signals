"""Publish broker-confirmed pending/layered paper trades without replaying Aug 18 history.

Day34's original member publication cutover predates the paper-critical layered/pending
execution path. That path proves broker placement with
``mt5.paper_critical_execution_success`` rather than ``mt5.day26_execution_success``.
Without this bridge, the trade can exist at Vantage while no root Telegram publication
or shared in-app notification is ever seeded.

The new proof type is deliberately forward-only from midnight 19 Aug 2026 in Bulgaria
(18 Aug 21:00 UTC). Older Aug 18 paper-critical trades remain historical evidence and
must not be replayed into the member feed after this fix deploys.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Callable

from sqlalchemy import text

from app.telegram_publisher_day34_cutover import Day34CutoverTelegramPublisherManager

_PAPER_CRITICAL_PUBLICATION_CUTOVER = datetime(2026, 8, 18, 21, 0, tzinfo=UTC)


def _seed_paper_critical_publications(manager: Any) -> None:
    """Seed only forward paper-critical broker placements and their member events."""

    with manager._session_factory() as session:
        publish_after = session.execute(
            text("SELECT publish_after FROM day34_summary_state WHERE id=1")
        ).scalar_one()
        params = {
            "publish_after": publish_after,
            "paper_critical_publish_after": _PAPER_CRITICAL_PUBLICATION_CUTOVER,
        }

        # Day34 may have suppressed a row before broker placement completed. Restore it
        # when the paper-critical path subsequently proves placement after this rollout.
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
                        AND placed.event_type='mt5.paper_critical_execution_success'
                        AND placed.created_at > :paper_critical_publish_after
                  )
                """
            ),
            params,
        )

        # Root Signal posts: this is the missing bridge exposed by TDC 6671/6676/6685
        # and today's TIG 597/603/607. Broker placement, not canonical creation alone,
        # remains the publication authority.
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
                      AND placed.event_type='mt5.paper_critical_execution_success'
                      AND placed.created_at > :paper_critical_publish_after
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
            params,
        )

        # Seed only lifecycle events from the new forward boundary. Day20/34 claim logic
        # still guarantees the root is sent before any reply beneath it.
        session.execute(
            text(
                """
                INSERT INTO telegram_publications (
                    signal_id, lifecycle_event_id, publication_kind, status
                )
                SELECT ev.signal_id, ev.id, 'lifecycle_event', 'pending'
                FROM signal_lifecycle_events AS ev
                WHERE ev.occurred_at > :paper_critical_publish_after
                  AND EXISTS (
                      SELECT 1
                      FROM audit_events AS placed
                      WHERE placed.entity_type='signal'
                        AND placed.entity_id=ev.signal_id
                        AND placed.event_type='mt5.paper_critical_execution_success'
                        AND placed.created_at > :paper_critical_publish_after
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM telegram_publications AS pub
                      WHERE pub.lifecycle_event_id=ev.id
                  )
                ON CONFLICT DO NOTHING
                """
            ),
            params,
        )

        # Keep in-app member notifications in parity with Telegram roots.
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
                    'SS-' || upper(left(replace(sig.id::text,'-',''),10)) || ' · ' ||
                        COALESCE(sig.symbol,'') || ' ' || COALESCE(sig.side,'') || ' opened',
                    'Trade placed and confirmed at the broker.',
                    jsonb_build_object(
                        'broker_confirmed', true,
                        'public_trade_reference', 'SS-' || upper(left(replace(sig.id::text,'-',''),10)),
                        'provider_identity_exposed', false,
                        'trade_action_created', false
                    )
                FROM signals AS sig
                WHERE EXISTS (
                    SELECT 1
                    FROM audit_events AS placed
                    WHERE placed.entity_type='signal'
                      AND placed.entity_id=sig.id
                      AND placed.event_type='mt5.paper_critical_execution_success'
                      AND placed.created_at > :paper_critical_publish_after
                )
                ON CONFLICT (event_key) DO NOTHING
                """
            ),
            params,
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
                    'SS-' || upper(left(replace(ev.signal_id::text,'-',''),10)) || ' · ' ||
                    CASE
                        WHEN ev.event_type='broker_result_win' THEN 'Trade won'
                        WHEN ev.event_type='broker_result_loss' THEN 'Trade lost'
                        WHEN ev.event_type='broker_result_breakeven' THEN 'Trade closed at break even'
                        WHEN ev.event_type='broker_result_closed' THEN 'Trade closed'
                        ELSE 'Trade update'
                    END,
                    'SS-' || upper(left(replace(ev.signal_id::text,'-',''),10)) || ' · ' || ev.rendered_text,
                    jsonb_build_object(
                        'origin', ev.origin,
                        'broker_result', ev.event_type LIKE 'broker_result_%',
                        'public_trade_reference', 'SS-' || upper(left(replace(ev.signal_id::text,'-',''),10)),
                        'provider_identity_exposed', false,
                        'trade_action_created', false
                    )
                FROM signal_lifecycle_events AS ev
                WHERE ev.occurred_at > :paper_critical_publish_after
                  AND EXISTS (
                      SELECT 1
                      FROM audit_events AS placed
                      WHERE placed.entity_type='signal'
                        AND placed.entity_id=ev.signal_id
                        AND placed.event_type='mt5.paper_critical_execution_success'
                        AND placed.created_at > :paper_critical_publish_after
                  )
                ON CONFLICT (event_key) DO NOTHING
                """
            ),
            params,
        )
        session.commit()


def install_paper_critical_publication_override() -> None:
    """Install the forward-only broker-proof bridge exactly once."""

    cls = Day34CutoverTelegramPublisherManager
    current = cls._seed_missing_publications
    if getattr(current, "_paper_critical_publication_support", False):
        return

    original: Callable[..., Any] = current

    def _seed_missing_publications(self: Any) -> None:
        original(self)
        _seed_paper_critical_publications(self)

    _seed_missing_publications._paper_critical_publication_support = True  # type: ignore[attr-defined]
    _seed_missing_publications._paper_critical_publication_cutover = (  # type: ignore[attr-defined]
        _PAPER_CRITICAL_PUBLICATION_CUTOVER
    )
    cls._seed_missing_publications = _seed_missing_publications


__all__ = [
    "_PAPER_CRITICAL_PUBLICATION_CUTOVER",
    "install_paper_critical_publication_override",
]
