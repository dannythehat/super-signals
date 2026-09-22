"""Canonical Super Signals Telegram/member publication manager.

Member output is a live mirror of broker-confirmed Super Signals activity, not a replay
queue for historical lifecycle rows. Broker/audit history remains durable in PostgreSQL,
but stale events are never converted into Telegram or in-app notifications after a
restart or deployment.

Root trade posts require a recent successful canonical broker route. Lifecycle posts and
in-app lifecycle notifications require a fresh lifecycle event. The pinned Live Trades
board remains broker-state based and can show older trades that are genuinely still
active without replaying their historical updates.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import text

from app.telegram_publisher import PublicationAttempt
from app.telegram_publisher_day20 import LifecyclePublicationAttempt
from app.telegram_publisher_day34 import SummaryPublicationAttempt
from app.telegram_publisher_day34_cutover import Day34CutoverTelegramPublisherManager
from app.trade_identity import prefix_public_trade_identity, public_trade_identity

_PLACEMENT_EVENT = "mt5.day38_route_new_trade"
_MEMBER_EVENT_FRESHNESS = timedelta(minutes=5)


def _decimal_text(value: Any) -> str:
    if value is None:
        return "N/A"
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return "N/A"
    if not parsed.is_finite():
        return "N/A"
    return format(parsed.normalize(), "f")


def _render_root(row: Any) -> str:
    """Render broker-confirmed execution without inventing provider evidence."""
    symbol = str(row["symbol"] or "").upper()
    side = str(row["side"] or "").upper()
    entry_low = row["entry_low"]
    entry_high = row["entry_high"]
    broker_entry = row["broker_entry"]
    if entry_low is not None and entry_high is not None:
        if Decimal(str(entry_low)) == Decimal(str(entry_high)):
            entry_text = _decimal_text(entry_low)
        else:
            entry_text = f"{_decimal_text(entry_low)} - {_decimal_text(entry_high)}"
    elif broker_entry is not None:
        entry_text = f"{_decimal_text(broker_entry)} (market)"
    else:
        entry_text = "Market"

    stop_loss = row["stop_loss"] if row["stop_loss"] is not None else row["broker_stop_loss"]
    take_profits = list(row["take_profits"] or [])
    if not take_profits:
        take_profits = list(row["broker_take_profits"] or [])

    lines = [
        f"{symbol} {side}",
        f"Entry: {entry_text}",
        f"Stop Loss: {_decimal_text(stop_loss)}",
    ]
    for index, target in enumerate(take_profits, start=1):
        lines.append(f"TP{index}: {_decimal_text(target)}")
    if bool(row["has_open_runner"]):
        lines.append(f"TP{len(take_profits) + 1}: OPEN")
    multiplier = Decimal(str(row["risk_multiplier"] or "1"))
    lines.append("Size: Double" if multiplier == Decimal("2") else "Size: Standard")
    return "\n".join(lines)


class CanonicalTelegramPublisherManager(Day34CutoverTelegramPublisherManager):
    """Fresh-only member publication path plus broker-backed live board."""

    @staticmethod
    def _placement_exists_sql(alias: str) -> str:
        return f"""
            EXISTS (
                SELECT 1
                FROM audit_events AS placed
                WHERE placed.entity_type='signal'
                  AND placed.entity_id={alias}
                  AND placed.event_type='{_PLACEMENT_EVENT}'
                  AND placed.payload->>'outcome'='executed'
                  AND placed.created_at>:fresh_after
            )
        """

    def _seed_missing_publications(self) -> None:
        """Seed only current member events; stale history remains audit-only."""
        fresh_after = datetime.now(UTC) - _MEMBER_EVENT_FRESHNESS
        with self._session_factory() as session:
            self._assign_member_trade_numbers(session, fresh_after=fresh_after)
            placement_for_pub = self._placement_exists_sql("pub.signal_id")
            placement_for_sig = self._placement_exists_sql("sig.id")
            placement_for_ev = self._placement_exists_sql("ev.signal_id")

            # Old unsent roots are not replayed after restart. A new root is publishable
            # only when its successful broker route itself is recent.
            session.execute(
                text(
                    f"""
                    UPDATE telegram_publications AS pub
                    SET status='suppressed',
                        failure_code='stale_or_unconfirmed_member_root',
                        failure_reason='Member trade roots are forward-only and require a recent confirmed broker route.',
                        updated_at=now()
                    WHERE pub.publication_kind='signal_created'
                      AND pub.lifecycle_event_id IS NULL
                      AND pub.status='pending'
                      AND NOT {placement_for_pub}
                    """
                ),
                {"fresh_after": fresh_after},
            )

            session.execute(
                text(
                    f"""
                    UPDATE telegram_publications AS pub
                    SET status='pending',failure_code=NULL,failure_reason=NULL,updated_at=now()
                    WHERE pub.publication_kind='signal_created'
                      AND pub.lifecycle_event_id IS NULL
                      AND pub.status='suppressed'
                      AND pub.failure_code IN (
                          'day34_not_broker_placed',
                          'canonical_not_broker_placed',
                          'stale_or_unconfirmed_member_root'
                      )
                      AND pub.telegram_message_id IS NULL
                      AND {placement_for_pub}
                    """
                ),
                {"fresh_after": fresh_after},
            )

            session.execute(
                text(
                    f"""
                    INSERT INTO telegram_publications(signal_id,publication_kind,status)
                    SELECT sig.id,'signal_created','pending'
                    FROM signals AS sig
                    WHERE {placement_for_sig}
                      AND NOT EXISTS (
                          SELECT 1 FROM telegram_publications AS pub
                          WHERE pub.signal_id=sig.id
                            AND pub.publication_kind='signal_created'
                            AND pub.lifecycle_event_id IS NULL
                      )
                    ON CONFLICT DO NOTHING
                    """
                ),
                {"fresh_after": fresh_after},
            )

            # Hard-stop any previously queued stale lifecycle rows before claim/send.
            session.execute(
                text(
                    """
                    UPDATE telegram_publications AS pub
                    SET status='suppressed',
                        failure_code='stale_lifecycle_audit_only',
                        failure_reason='Historical lifecycle events are audit-only and are never replayed to members.',
                        updated_at=now()
                    FROM signal_lifecycle_events AS ev
                    WHERE pub.lifecycle_event_id=ev.id
                      AND pub.publication_kind='lifecycle_event'
                      AND pub.status='pending'
                      AND ev.created_at<:fresh_after
                    """
                ),
                {"fresh_after": fresh_after},
            )

            session.execute(
                text(
                    f"""
                    INSERT INTO telegram_publications(
                        signal_id,lifecycle_event_id,publication_kind,status
                    )
                    SELECT ev.signal_id,ev.id,'lifecycle_event','pending'
                    FROM signal_lifecycle_events AS ev
                    WHERE ev.created_at>=:fresh_after
                      AND (
                          EXISTS (
                              SELECT 1 FROM telegram_publications AS root
                              WHERE root.signal_id=ev.signal_id
                                AND root.publication_kind='signal_created'
                                AND root.lifecycle_event_id IS NULL
                                AND root.status='sent'
                                AND root.telegram_message_id IS NOT NULL
                          )
                          OR {placement_for_ev}
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM telegram_publications AS pub
                          WHERE pub.lifecycle_event_id=ev.id
                      )
                    ON CONFLICT DO NOTHING
                    """
                ),
                {"fresh_after": fresh_after},
            )

            self._seed_fresh_in_app_notifications(session, fresh_after=fresh_after)
            self._suppress_stale_in_app_replays(session)
            session.commit()

        if self._summary_service is not None:
            self._summary_service.seed_due()
        self._seed_summary_deliveries()
        self._sync_live_board_safely()

    @staticmethod
    def _assign_member_trade_numbers(session: Any, *, fresh_after: datetime) -> None:
        """Assign the lowest free active slot and keep it for the trade lifecycle."""

        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('super-signals-member-trade-numbers'))")
        )
        active_sql = """
            EXISTS (
                SELECT 1
                FROM positions AS p
                LEFT JOIN performance_trade_outcomes AS o ON o.position_id=p.id
                WHERE p.signal_id=sig.id
                  AND (
                      o.status='pending'
                      OR (
                          COALESCE(o.status,'open')
                              NOT IN ('won','lost','breakeven','closed_unknown')
                          AND (
                              (p.status='open' AND p.broker_position_id IS NOT NULL)
                              OR (
                                  p.status IN ('planned','pending')
                                  AND COALESCE(p.broker_order_id,p.broker_position_id)
                                      IS NOT NULL
                              )
                          )
                      )
                  )
            )
        """
        used = {
            int(value)
            for value in session.execute(
                text(
                    f"""
                    SELECT DISTINCT sig.member_trade_number
                    FROM signals AS sig
                    WHERE sig.member_trade_number IS NOT NULL
                      AND {active_sql}
                    """
                )
            ).scalars()
            if value is not None
        }
        candidates = session.execute(
            text(
                f"""
                SELECT sig.id
                FROM signals AS sig
                WHERE sig.member_trade_number IS NULL
                  AND (
                      sig.created_at>=:fresh_after
                      OR {active_sql}
                  )
                  AND EXISTS (
                      SELECT 1
                      FROM audit_events AS placed
                      WHERE placed.entity_type='signal'
                        AND placed.entity_id=sig.id
                        AND (
                            placed.event_type='mt5.day26_execution_success'
                            OR (
                                placed.event_type='{_PLACEMENT_EVENT}'
                                AND placed.payload->>'outcome'='executed'
                            )
                        )
                  )
                ORDER BY sig.created_at,sig.id
                FOR UPDATE
                """
            ),
            {"fresh_after": fresh_after},
        ).scalars().all()
        for signal_id in candidates:
            number = 1
            while number in used:
                number += 1
            session.execute(
                text(
                    """
                    UPDATE signals
                    SET member_trade_number=:number,updated_at=now()
                    WHERE id=:signal_id AND member_trade_number IS NULL
                    """
                ),
                {"signal_id": signal_id, "number": number},
            )
            used.add(number)

    @staticmethod
    def _seed_fresh_in_app_notifications(session: Any, *, fresh_after: datetime) -> None:
        session.execute(
            text(
                f"""
                INSERT INTO notification_events(
                    event_key,signal_id,lifecycle_event_id,user_id,
                    audience,kind,title,body,payload
                )
                SELECT
                    'signal-open:' || sig.id::text,
                    sig.id,NULL,NULL,'shared','trade_open',
                    'NEW TRADE PLACED — TRADE ' || sig.member_trade_number::text,
                    COALESCE(sig.symbol,'') || ' ' || COALESCE(sig.side,'') ||
                        ' placed and confirmed at the broker.',
                    jsonb_build_object(
                        'broker_confirmed',true,
                        'canonical_route',true,
                        'public_trade_reference','TRADE ' || sig.member_trade_number::text,
                        'canonical_signal_reference','SS-' || upper(left(replace(sig.id::text,'-',''),10)),
                        'provider_identity_exposed',false,
                        'trade_action_created',false
                    )
                FROM signals AS sig
                WHERE EXISTS (
                    SELECT 1 FROM audit_events AS placed
                    WHERE placed.entity_type='signal'
                      AND placed.entity_id=sig.id
                      AND placed.event_type='{_PLACEMENT_EVENT}'
                      AND placed.payload->>'outcome'='executed'
                      AND placed.created_at>:fresh_after
                )
                ON CONFLICT (event_key) DO NOTHING
                """
            ),
            {"fresh_after": fresh_after},
        )
        session.execute(
            text(
                """
                INSERT INTO notification_events(
                    event_key,signal_id,lifecycle_event_id,user_id,
                    audience,kind,title,body,payload
                )
                SELECT
                    'lifecycle:' || ev.id::text,
                    ev.signal_id,ev.id,NULL,'shared',
                    CASE WHEN ev.event_type LIKE 'broker_result_%'
                         THEN 'trade_result' ELSE 'trade_update' END,
                    'TRADE ' || sig.member_trade_number::text || ' · ' ||
                    CASE
                        WHEN ev.event_type='broker_result_win' THEN 'CLOSED — WIN'
                        WHEN ev.event_type='broker_result_loss' THEN 'CLOSED — LOSS'
                        WHEN ev.event_type='broker_result_breakeven' THEN 'CLOSED — BREAK EVEN'
                        WHEN ev.event_type='broker_result_closed' THEN 'CLOSED'
                        ELSE 'UPDATE'
                    END,
                    'TRADE ' || sig.member_trade_number::text || ' · ' || ev.rendered_text,
                    jsonb_build_object(
                        'origin',ev.origin,
                        'broker_result',ev.event_type LIKE 'broker_result_%',
                        'public_trade_reference','TRADE ' || sig.member_trade_number::text,
                        'canonical_signal_reference','SS-' || upper(left(replace(ev.signal_id::text,'-',''),10)),
                        'provider_identity_exposed',false,
                        'trade_action_created',false
                    )
                FROM signal_lifecycle_events AS ev
                JOIN signals AS sig ON sig.id=ev.signal_id
                WHERE ev.occurred_at>=:fresh_after
                  AND ev.created_at>=:fresh_after
                  AND EXISTS (
                      SELECT 1 FROM telegram_publications AS root
                      WHERE root.signal_id=ev.signal_id
                        AND root.publication_kind='signal_created'
                        AND root.lifecycle_event_id IS NULL
                        AND root.status IN ('sent','pending','sending')
                  )
                ON CONFLICT (event_key) DO NOTHING
                """
            ),
            {"fresh_after": fresh_after},
        )

    @staticmethod
    def _suppress_stale_in_app_replays(session: Any) -> None:
        """Hide previously replayed stale rows while retaining them for forensic audit."""
        session.execute(
            text(
                """
                UPDATE notification_events AS n
                SET audience='suppressed'
                FROM signal_lifecycle_events AS ev
                WHERE n.lifecycle_event_id=ev.id
                  AND n.audience='shared'
                  AND n.created_at>ev.occurred_at+interval '5 minutes'
                """
            )
        )

    def _claim_next(self) -> PublicationAttempt | SummaryPublicationAttempt | None:
        root = self._claim_root()
        if root is not None:
            return root
        lifecycle = self._claim_lifecycle()
        if lifecycle is not None:
            return lifecycle
        return self._claim_summary()

    def _claim_root(self) -> PublicationAttempt | None:
        assert self._destination_chat_id is not None
        fresh_after = datetime.now(UTC) - _MEMBER_EVENT_FRESHNESS
        with self._session_factory() as session:
            row = session.execute(
                text(
                    f"""
                    SELECT
                        pub.id AS publication_id,pub.signal_id,sig.member_trade_number,
                        sig.symbol,sig.side,sig.entry_low,sig.entry_high,sig.stop_loss,
                        sig.take_profits,sig.has_open_runner,sig.risk_multiplier,
                        (SELECT MIN(p.entry_price) FROM positions p
                         WHERE p.signal_id=sig.id AND p.entry_price IS NOT NULL) AS broker_entry,
                        (SELECT MIN(p.stop_loss) FROM positions p
                         WHERE p.signal_id=sig.id AND p.stop_loss IS NOT NULL) AS broker_stop_loss,
                        (SELECT ARRAY_AGG(DISTINCT p.take_profit ORDER BY p.take_profit)
                         FROM positions p
                         WHERE p.signal_id=sig.id AND p.take_profit IS NOT NULL) AS broker_take_profits
                    FROM telegram_publications AS pub
                    JOIN signals AS sig ON sig.id=pub.signal_id
                    WHERE pub.status='pending'
                      AND pub.publication_kind='signal_created'
                      AND pub.lifecycle_event_id IS NULL
                      AND {self._placement_exists_sql('pub.signal_id')}
                    ORDER BY pub.created_at,pub.id
                    FOR UPDATE OF pub SKIP LOCKED
                    LIMIT 1
                    """
                ),
                {"fresh_after": fresh_after},
            ).mappings().first()
            if row is None:
                session.rollback()
                return None
            identity = public_trade_identity(row["signal_id"], row["member_trade_number"])
            rendered = f"🚨 NEW TRADE PLACED — {identity.label}\n\n{_render_root(row)}"
            session.execute(
                text(
                    """
                    UPDATE telegram_publications
                    SET status='sending',rendered_text=:rendered_text,
                        destination_chat_id=:destination_chat_id,
                        attempt_count=attempt_count+1,attempted_at=now(),
                        failure_code=NULL,failure_reason=NULL,updated_at=now()
                    WHERE id=:publication_id AND status='pending'
                    """
                ),
                {
                    "publication_id": row["publication_id"],
                    "rendered_text": rendered,
                    "destination_chat_id": self._destination_chat_id,
                },
            )
            session.commit()
            return PublicationAttempt(
                publication_id=row["publication_id"],
                signal_id=row["signal_id"],
                text=rendered,
            )

    def _claim_lifecycle(self) -> LifecyclePublicationAttempt | None:
        assert self._destination_chat_id is not None
        fresh_after = datetime.now(UTC) - _MEMBER_EVENT_FRESHNESS
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT
                        pub.id AS publication_id,pub.signal_id,sig.member_trade_number,
                        ev.rendered_text,
                        root.telegram_message_id AS reply_to_message_id
                    FROM telegram_publications AS pub
                    JOIN signal_lifecycle_events AS ev ON ev.id=pub.lifecycle_event_id
                    JOIN signals AS sig ON sig.id=pub.signal_id
                    JOIN telegram_publications AS root
                      ON root.signal_id=pub.signal_id
                     AND root.publication_kind='signal_created'
                     AND root.lifecycle_event_id IS NULL
                    WHERE pub.status='pending'
                      AND pub.publication_kind='lifecycle_event'
                      AND root.status='sent'
                      AND root.telegram_message_id IS NOT NULL
                      AND ev.occurred_at>=:fresh_after
                      AND ev.created_at>=:fresh_after
                    ORDER BY ev.occurred_at,ev.created_at,pub.id
                    FOR UPDATE OF pub SKIP LOCKED
                    LIMIT 1
                    """
                ),
                {"fresh_after": fresh_after},
            ).mappings().first()
            if row is None:
                session.rollback()
                return None
            rendered = prefix_public_trade_identity(
                row["signal_id"],
                str(row["rendered_text"] or "Trade update."),
                row["member_trade_number"],
            )
            reply_id = int(row["reply_to_message_id"])
            session.execute(
                text(
                    """
                    UPDATE telegram_publications
                    SET status='sending',rendered_text=:rendered_text,
                        destination_chat_id=:destination_chat_id,
                        reply_to_telegram_message_id=:reply_to_message_id,
                        attempt_count=attempt_count+1,attempted_at=now(),
                        failure_code=NULL,failure_reason=NULL,updated_at=now()
                    WHERE id=:publication_id AND status='pending'
                    """
                ),
                {
                    "publication_id": row["publication_id"],
                    "rendered_text": rendered,
                    "destination_chat_id": self._destination_chat_id,
                    "reply_to_message_id": reply_id,
                },
            )
            session.commit()
            return LifecyclePublicationAttempt(
                publication_id=row["publication_id"],
                signal_id=row["signal_id"],
                text=rendered,
                reply_to_message_id=reply_id,
            )

    def _claim_summary(self) -> SummaryPublicationAttempt | None:
        assert self._destination_chat_id is not None
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT d.id AS delivery_id,d.notification_id,n.title,n.body
                    FROM telegram_notification_deliveries AS d
                    JOIN notification_events AS n ON n.id=d.notification_id
                    WHERE d.status='pending'
                      AND n.audience!='suppressed'
                    ORDER BY d.created_at,d.id
                    FOR UPDATE OF d SKIP LOCKED
                    LIMIT 1
                    """
                )
            ).mappings().first()
            if row is None:
                session.rollback()
                return None
            rendered = f"{str(row['title'])}\n{str(row['body'])}"
            session.execute(
                text(
                    """
                    UPDATE telegram_notification_deliveries
                    SET status='sending',rendered_text=:rendered,
                        destination_chat_id=:chat_id,
                        attempt_count=attempt_count+1,attempted_at=now(),
                        failure_code=NULL,failure_reason=NULL,updated_at=now()
                    WHERE id=:delivery_id AND status='pending'
                    """
                ),
                {
                    "delivery_id": row["delivery_id"],
                    "rendered": rendered,
                    "chat_id": self._destination_chat_id,
                },
            )
            session.commit()
            return SummaryPublicationAttempt(
                delivery_id=row["delivery_id"],
                notification_id=row["notification_id"],
                text=rendered,
            )

    def _live_board_rows(self) -> list[Any]:
        """Show broker-active Signals regardless of their original publication age."""
        with self._session_factory() as session:
            return list(
                session.execute(
                    text(
                        f"""
                        WITH leg_state AS (
                            SELECT
                                s.id AS signal_id,
                                COALESCE(s.symbol,'') AS symbol,
                                COALESCE(s.side,'') AS side,
                                p.tp_index,
                                CASE
                                    WHEN o.status IN ('won','lost','breakeven','closed_unknown') THEN 'closed'
                                    WHEN o.status='pending' THEN 'pending'
                                    WHEN p.status='open' AND p.broker_position_id IS NOT NULL THEN 'open'
                                    ELSE 'closed'
                                END AS effective_status
                            FROM positions AS p
                            JOIN signals AS s ON s.id=p.signal_id
                            LEFT JOIN performance_trade_outcomes AS o ON o.position_id=p.id
                            WHERE EXISTS (
                                SELECT 1 FROM audit_events AS placed
                                WHERE placed.entity_type='signal'
                                  AND placed.entity_id=s.id
                                  AND placed.event_type='{_PLACEMENT_EVENT}'
                                  AND placed.payload->>'outcome'='executed'
                            )
                        )
                        SELECT
                            signal_id,MAX(symbol) AS symbol,MAX(side) AS side,
                            ARRAY_AGG(DISTINCT tp_index ORDER BY tp_index)
                                FILTER (WHERE effective_status='open') AS open_tp_indices,
                            ARRAY_AGG(DISTINCT tp_index ORDER BY tp_index)
                                FILTER (WHERE effective_status='pending') AS pending_tp_indices
                        FROM leg_state
                        GROUP BY signal_id
                        HAVING BOOL_OR(effective_status='open') OR BOOL_OR(effective_status='pending')
                        ORDER BY signal_id
                        """
                    )
                ).mappings().all()
            )

    @staticmethod
    def _render_live_board(rows: list[Any]) -> str:
        open_count = sum(1 for row in rows if row["open_tp_indices"])
        pending_count = sum(1 for row in rows if row["pending_tp_indices"])
        lines = [
            "📌 SUPER SIGNALS · LIVE TRADES",
            f"OPEN {open_count} · PENDING {pending_count}",
        ]
        if not rows:
            lines.extend(["", "No active trades."])
            return "\n".join(lines)
        lines.append("")
        for row in rows:
            identity = public_trade_identity(row["signal_id"])
            symbol = str(row["symbol"] or "").upper()
            side = str(row["side"] or "").upper()
            open_indices = [int(value) for value in (row["open_tp_indices"] or [])]
            pending_indices = [int(value) for value in (row["pending_tp_indices"] or [])]
            states: list[str] = []
            if open_indices:
                states.append("/".join(f"TP{index}" for index in open_indices) + " open")
            if pending_indices:
                states.append("/".join(f"TP{index}" for index in pending_indices) + " pending")
            lines.append(f"{identity.label} · {symbol} {side} · {' · '.join(states)}")
        return "\n".join(lines)


__all__ = ["CanonicalTelegramPublisherManager"]
