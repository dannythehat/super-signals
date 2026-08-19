"""Canonical Super Signals Telegram/member publication manager.

The publisher consumes one broker-placement truth: the shared Day38 route audit with
``outcome=executed``. It does not care whether the underlying executor used market,
pending or layered placement, so those paths cannot silently disappear from Telegram.

Root and lifecycle posts are claimed directly here rather than through legacy render
helpers. Sparse canonical values are safe, broker-mapped execution values fill display
fields where the provider intentionally supplied none, and any event older than five
minutes is unmistakably labelled DELAYED. Publishing remains a one-way mirror and can
never place or manage a trade.
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
_DELAY_THRESHOLD = timedelta(minutes=5)


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


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _age_text(age: timedelta) -> str:
    seconds = max(0, int(age.total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{max(1, minutes)}m"


def _mark_delayed(text_value: str, occurred_at: datetime | None) -> str:
    if occurred_at is None:
        return text_value
    age = datetime.now(UTC) - _utc(occurred_at)
    if age <= _DELAY_THRESHOLD or text_value.startswith("⏱ DELAYED UPDATE"):
        return text_value
    return f"⏱ DELAYED UPDATE · original event {_age_text(age)} earlier\n{text_value}"


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
        "SUPER SIGNALS",
        "",
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
    """One broker-route-driven publication path for roots, updates and summaries."""

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
                  AND placed.created_at>:publish_after
            )
        """

    def _seed_missing_publications(self) -> None:
        """Seed from the shared successful broker route, never executor-specific events."""
        with self._session_factory() as session:
            publish_after = session.execute(
                text("SELECT publish_after FROM day34_summary_state WHERE id=1")
            ).scalar_one()

            placement_for_pub = self._placement_exists_sql("pub.signal_id")
            placement_for_sig = self._placement_exists_sql("sig.id")
            placement_for_ev = self._placement_exists_sql("ev.signal_id")

            session.execute(
                text(
                    f"""
                    UPDATE telegram_publications AS pub
                    SET status='suppressed',
                        failure_code='canonical_not_broker_placed',
                        failure_reason='Member roots require a confirmed canonical broker route.',
                        updated_at=now()
                    WHERE pub.publication_kind='signal_created'
                      AND pub.lifecycle_event_id IS NULL
                      AND pub.status='pending'
                      AND NOT {placement_for_pub}
                    """
                ),
                {"publish_after": publish_after},
            )

            session.execute(
                text(
                    f"""
                    UPDATE telegram_publications AS pub
                    SET status='pending',failure_code=NULL,failure_reason=NULL,updated_at=now()
                    WHERE pub.publication_kind='signal_created'
                      AND pub.lifecycle_event_id IS NULL
                      AND pub.status='suppressed'
                      AND pub.failure_code IN ('day34_not_broker_placed','canonical_not_broker_placed')
                      AND pub.telegram_message_id IS NULL
                      AND {placement_for_pub}
                    """
                ),
                {"publish_after": publish_after},
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
                {"publish_after": publish_after},
            )

            session.execute(
                text(
                    f"""
                    INSERT INTO telegram_publications(
                        signal_id,lifecycle_event_id,publication_kind,status
                    )
                    SELECT ev.signal_id,ev.id,'lifecycle_event','pending'
                    FROM signal_lifecycle_events AS ev
                    WHERE ev.occurred_at>:publish_after
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
                {"publish_after": publish_after},
            )

            self._seed_canonical_in_app_notifications(session, publish_after=publish_after)
            session.commit()

        if self._summary_service is not None:
            self._summary_service.seed_due()
        self._seed_summary_deliveries()
        self._sync_live_board_safely()

    @staticmethod
    def _seed_canonical_in_app_notifications(session: Any, *, publish_after: datetime) -> None:
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
                    'SS-' || upper(left(replace(sig.id::text,'-',''),10)) || ' · ' ||
                        COALESCE(sig.symbol,'') || ' ' || COALESCE(sig.side,'') || ' opened',
                    'Trade placed and confirmed at the broker.',
                    jsonb_build_object(
                        'broker_confirmed',true,
                        'canonical_route',true,
                        'public_trade_reference','SS-' || upper(left(replace(sig.id::text,'-',''),10)),
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
                      AND placed.created_at>:publish_after
                )
                ON CONFLICT (event_key) DO NOTHING
                """
            ),
            {"publish_after": publish_after},
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
                        'origin',ev.origin,
                        'broker_result',ev.event_type LIKE 'broker_result_%',
                        'public_trade_reference','SS-' || upper(left(replace(ev.signal_id::text,'-',''),10)),
                        'provider_identity_exposed',false,
                        'trade_action_created',false
                    )
                FROM signal_lifecycle_events AS ev
                WHERE ev.occurred_at>:publish_after
                  AND EXISTS (
                      SELECT 1 FROM telegram_publications AS root
                      WHERE root.signal_id=ev.signal_id
                        AND root.publication_kind='signal_created'
                        AND root.lifecycle_event_id IS NULL
                        AND (
                            root.status='sent'
                            OR root.status IN ('pending','sending')
                        )
                  )
                ON CONFLICT (event_key) DO NOTHING
                """
            ),
            {"publish_after": publish_after},
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
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT
                        pub.id AS publication_id,pub.signal_id,
                        sig.symbol,sig.side,sig.entry_low,sig.entry_high,sig.stop_loss,
                        sig.take_profits,sig.has_open_runner,sig.risk_multiplier,
                        sig.source_posted_at,
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
                    ORDER BY pub.created_at,pub.id
                    FOR UPDATE OF pub SKIP LOCKED
                    LIMIT 1
                    """
                )
            ).mappings().first()
            if row is None:
                session.rollback()
                return None
            rendered = prefix_public_trade_identity(
                row["signal_id"],
                _mark_delayed(_render_root(row), row["source_posted_at"]),
            )
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
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT
                        pub.id AS publication_id,pub.signal_id,
                        ev.rendered_text,ev.occurred_at,
                        root.telegram_message_id AS reply_to_message_id
                    FROM telegram_publications AS pub
                    JOIN signal_lifecycle_events AS ev ON ev.id=pub.lifecycle_event_id
                    JOIN telegram_publications AS root
                      ON root.signal_id=pub.signal_id
                     AND root.publication_kind='signal_created'
                     AND root.lifecycle_event_id IS NULL
                    WHERE pub.status='pending'
                      AND pub.publication_kind='lifecycle_event'
                      AND root.status='sent'
                      AND root.telegram_message_id IS NOT NULL
                    ORDER BY ev.occurred_at,ev.created_at,pub.id
                    FOR UPDATE OF pub SKIP LOCKED
                    LIMIT 1
                    """
                )
            ).mappings().first()
            if row is None:
                session.rollback()
                return None
            rendered = prefix_public_trade_identity(
                row["signal_id"],
                _mark_delayed(str(row["rendered_text"] or "Trade update."), row["occurred_at"]),
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
        """Show broker-active Signals regardless of underlying market/pending executor."""
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
