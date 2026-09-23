# ruff: noqa: E501
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

import asyncio
import html
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.telegram_publisher import PublicationAttempt, TelegramPublishError, _bot_api_call
from app.telegram_publisher_day20 import LifecyclePublicationAttempt
from app.telegram_publisher_day34 import Day34TelegramPublisherManager, SummaryPublicationAttempt
from app.telegram_publisher_day34_cutover import Day34CutoverTelegramPublisherManager
from app.telegram_trade_ledger import TelegramTradeLedger, money, provider_badge
from app.trade_identity import public_trade_identity

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


def _html(value: Any) -> str:
    return html.escape(str(value), quote=False)


def _balance_money(value: Any) -> str:
    if value is None:
        return "unavailable"
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return "unavailable"
    return "$" + f"{amount:,.2f}"


def _clean_update_text(value: Any) -> str:
    lines: list[str] = []
    for raw in str(value or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line in {
            "TRADE UPDATE",
            "Instructions received:",
            "Broker execution confirmation follows separately.",
        }:
            continue
        if line == "SL moved to entry — trade remains open with break-even protection.":
            return "SL moved to entry."
        if line == "Provider completed trade details in the original Telegram message.":
            return "Trade details updated."
        if line.startswith("• "):
            line = "• " + line[2:].rstrip(".")
        lines.append(line)
    return "\n".join(lines[:4]) or "Trade updated."


def _render_root(row: Any) -> str:
    """Render only the details members actually need for a newly placed trade."""
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
        f"📍 <b>ENTRY</b>   {_html(entry_text)}",
        f"🛑 <b>STOP</b>    {_html(_decimal_text(stop_loss))}",
        "",
    ]
    for index, target in enumerate(take_profits, start=1):
        lines.append(f"🎯 <b>TP{index}</b>     {_html(_decimal_text(target))}")
    if bool(row["has_open_runner"]):
        lines.append(f"🏃 <b>TP{len(take_profits) + 1}</b>     OPEN")
    return "\n".join(lines)


class CanonicalTelegramPublisherManager(Day34CutoverTelegramPublisherManager):
    def _sync_live_board(self) -> None:
        """Strict no-spam board policy.

        The canonical board may edit its one stored Telegram message in place, and it may
        create the board only when no message has ever been recorded. It must never create
        a replacement post after an edit/pin failure. A stale/missing Telegram message is
        an operations problem, not permission to spam the member channel.
        """
        Day34TelegramPublisherManager._sync_live_board(self)

    """Fresh-only member publication path plus broker-backed live board."""

    def __init__(self, *, reference_user_id: UUID | None = None, **kwargs: Any) -> None:
        super().__init__(reference_user_id=reference_user_id, **kwargs)
        self._reference_user_id = reference_user_id
        self._trade_ledger = (
            TelegramTradeLedger(self._session_factory, reference_user_id)
            if reference_user_id is not None
            else None
        )

    def _account_lines(self, *, include_origin: bool = False) -> list[str]:
        if self._trade_ledger is None:
            return []
        return self._trade_ledger.account_lines(
            self._trade_ledger.account(),
            include_origin=include_origin,
        )

    @staticmethod
    def _provider_line(source_id: UUID | str | None, provider_name: str) -> str:
        return f"{provider_badge(source_id)} <b>{_html(provider_name)}</b>"

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

            # A final broker_result_* is the clean member-facing close event. When the
            # settlement sweep creates several leg-level rows for the same close burst,
            # suppress those components and publish one final result instead.
            session.execute(
                text(
                    """
                    UPDATE telegram_publications AS pub
                    SET status='suppressed',
                        failure_code='component_settlement_collapsed_into_final_result',
                        failure_reason='Final broker result carries the complete trade outcome.',
                        updated_at=now()
                    FROM signal_lifecycle_events AS ev
                    WHERE pub.lifecycle_event_id=ev.id
                      AND pub.status='pending'
                      AND ev.event_type='broker_position_settled'
                      AND EXISTS (
                          SELECT 1
                          FROM signal_lifecycle_events AS sibling
                          WHERE sibling.signal_id=ev.signal_id
                            AND sibling.event_type LIKE 'broker_result_%'
                            AND sibling.created_at>=:fresh_after
                            AND ABS(EXTRACT(EPOCH FROM (sibling.occurred_at-ev.occurred_at)))<=30
                      )
                    """
                ),
                {"fresh_after": fresh_after},
            )

            # Once the broker has declared the trade complete, later provider wording
            # must never resurrect it as another member update. Keep the evidence in
            # PostgreSQL but suppress the Telegram publication.
            session.execute(
                text(
                    """
                    UPDATE telegram_publications AS pub
                    SET status='suppressed',
                        failure_code='post_completion_lifecycle_noise',
                        failure_reason='Trade was already broker-complete before this update.',
                        updated_at=now()
                    FROM signal_lifecycle_events AS ev
                    WHERE pub.lifecycle_event_id=ev.id
                      AND pub.status='pending'
                      AND ev.event_type<>'broker_position_settled'
                      AND ev.event_type NOT LIKE 'broker_result_%'
                      AND EXISTS (
                          SELECT 1
                          FROM signal_lifecycle_events AS completed
                          WHERE completed.signal_id=ev.signal_id
                            AND completed.event_type LIKE 'broker_result_%'
                            AND completed.created_at<=ev.created_at
                      )
                    """
                )
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
                      AND NOT (
                          ev.event_type='broker_position_settled'
                          AND EXISTS (
                              SELECT 1
                              FROM signal_lifecycle_events AS sibling
                              WHERE sibling.signal_id=ev.signal_id
                                AND sibling.event_type LIKE 'broker_result_%'
                                AND sibling.created_at>=:fresh_after
                                AND ABS(EXTRACT(EPOCH FROM (sibling.occurred_at-ev.occurred_at)))<=30
                          )
                      )
                      AND NOT (
                          ev.event_type<>'broker_position_settled'
                          AND ev.event_type NOT LIKE 'broker_result_%'
                          AND EXISTS (
                              SELECT 1
                              FROM signal_lifecycle_events AS completed
                              WHERE completed.signal_id=ev.signal_id
                                AND completed.event_type LIKE 'broker_result_%'
                                AND completed.created_at<=ev.created_at
                          )
                      )
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
                    COALESCE(NULLIF(src.chat_title,''),src.source_alias,'Unknown provider') ||
                        ' · ' || COALESCE(sig.symbol,'') || ' ' || COALESCE(sig.side,'') ||
                        ' placed and confirmed at the broker.',
                    jsonb_build_object(
                        'broker_confirmed',true,
                        'canonical_route',true,
                        'public_trade_reference','TRADE ' || sig.member_trade_number::text,
                        'canonical_signal_reference','SS-' || upper(left(replace(sig.id::text,'-',''),10)),
                        'provider_identity_exposed',true,
                        'provider_name',COALESCE(NULLIF(src.chat_title,''),src.source_alias,'Unknown provider'),
                        'trade_action_created',false
                    )
                FROM signals AS sig
                JOIN sources AS src ON src.id=sig.source_id
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
                        'provider_identity_exposed',true,
                        'provider_name',COALESCE(NULLIF(src.chat_title,''),src.source_alias,'Unknown provider'),
                        'trade_action_created',false
                    )
                FROM signal_lifecycle_events AS ev
                JOIN signals AS sig ON sig.id=ev.signal_id
                JOIN sources AS src ON src.id=sig.source_id
                WHERE ev.created_at>=:fresh_after
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
                  AND n.created_at>ev.created_at+interval '5 minutes'
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
                        sig.source_id,
                        COALESCE(NULLIF(src.chat_title,''),src.source_alias,'Unknown provider')
                            AS provider_name,
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
                    JOIN sources AS src ON src.id=sig.source_id
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
            provider_line = self._provider_line(
                row["source_id"], str(row["provider_name"] or "Unknown provider")
            )
            side = str(row["side"] or "").upper()
            symbol = str(row["symbol"] or "").upper()
            side_icon = "🟢" if side == "BUY" else "🔴" if side == "SELL" else "⚪"
            parts = [
                provider_line,
                "",
                f"{_html(identity.marker)} <b>{_html(identity.reference)} · NEW TRADE</b>",
                f"{side_icon} <b>{_html(symbol)} {_html(side)}</b>",
                "",
                _render_root(row),
            ]
            rendered = "\n".join(parts)
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
                        sig.source_id,
                        COALESCE(NULLIF(src.chat_title,''),src.source_alias,'Unknown provider')
                            AS provider_name,
                        ev.event_type,ev.rendered_text,ev.aggregate_result,
                        event_position.tp_index AS event_tp_index,
                        event_outcome.cash_pnl AS event_cash_pnl,
                        event_outcome.status AS event_outcome_status,
                        root.telegram_message_id AS reply_to_message_id
                    FROM telegram_publications AS pub
                    JOIN signal_lifecycle_events AS ev ON ev.id=pub.lifecycle_event_id
                    JOIN signals AS sig ON sig.id=pub.signal_id
                    JOIN sources AS src ON src.id=sig.source_id
                    LEFT JOIN positions AS event_position
                      ON event_position.id::text=COALESCE(ev.aggregate_result->>'position_id','')
                    LEFT JOIN performance_trade_outcomes AS event_outcome
                      ON event_outcome.position_id=event_position.id
                     AND event_outcome.user_id=:reference_user_id
                    JOIN telegram_publications AS root
                      ON root.signal_id=pub.signal_id
                     AND root.publication_kind='signal_created'
                     AND root.lifecycle_event_id IS NULL
                    WHERE pub.status='pending'
                      AND pub.publication_kind='lifecycle_event'
                      AND root.status='sent'
                      AND root.telegram_message_id IS NOT NULL
                      AND ev.created_at>=:fresh_after
                      AND NOT (
                          ev.event_type='broker_position_settled'
                          AND EXISTS (
                              SELECT 1
                              FROM signal_lifecycle_events AS sibling
                              WHERE sibling.signal_id=ev.signal_id
                                AND sibling.event_type LIKE 'broker_result_%'
                                AND sibling.created_at>=:fresh_after
                                AND ABS(EXTRACT(EPOCH FROM (sibling.occurred_at-ev.occurred_at)))<=30
                          )
                      )
                      AND NOT (
                          ev.event_type<>'broker_position_settled'
                          AND ev.event_type NOT LIKE 'broker_result_%'
                          AND EXISTS (
                              SELECT 1
                              FROM signal_lifecycle_events AS completed
                              WHERE completed.signal_id=ev.signal_id
                                AND completed.event_type LIKE 'broker_result_%'
                                AND completed.created_at<=ev.created_at
                          )
                      )
                    ORDER BY ev.occurred_at,ev.created_at,pub.id
                    FOR UPDATE OF pub SKIP LOCKED
                    LIMIT 1
                    """
                ),
                {"fresh_after": fresh_after, "reference_user_id": self._reference_user_id},
            ).mappings().first()
            if row is None:
                session.rollback()
                return None
            identity = public_trade_identity(row["signal_id"], row["member_trade_number"])
            provider_line = self._provider_line(
                row["source_id"], str(row["provider_name"] or "Unknown provider")
            )
            trade = self._trade_ledger.trade(row["signal_id"]) if self._trade_ledger else None
            account = self._trade_ledger.account() if self._trade_ledger else None
            event_type = str(row["event_type"] or "")
            aggregate = row["aggregate_result"] if isinstance(row["aggregate_result"], dict) else {}
            event_outcome = str(
                row["event_outcome_status"] or aggregate.get("outcome") or ""
            ).lower()
            raw_tp = row["event_tp_index"] if row["event_tp_index"] is not None else aggregate.get("tp_index")
            try:
                tp_index = int(raw_tp) if raw_tp is not None else None
            except (TypeError, ValueError):
                tp_index = None
            tp_label = f"TP{tp_index}" if tp_index is not None else "POSITION"
            event_pnl = row["event_cash_pnl"]

            has_more_settlements = bool(
                session.execute(
                    text(
                        """
                        SELECT 1
                        FROM telegram_publications AS next_pub
                        JOIN signal_lifecycle_events AS next_ev
                          ON next_ev.id=next_pub.lifecycle_event_id
                        WHERE next_pub.signal_id=:signal_id
                          AND next_pub.id<>:publication_id
                          AND next_pub.status='pending'
                          AND next_ev.event_type='broker_position_settled'
                        LIMIT 1
                        """
                    ),
                    {
                        "signal_id": row["signal_id"],
                        "publication_id": row["publication_id"],
                    },
                ).scalar_one_or_none()
            )
            final_settlement = (
                event_type == "broker_position_settled"
                and trade is not None
                and trade.complete
                and not has_more_settlements
            )

            if event_type == "broker_position_settled":
                if event_outcome == "won":
                    result_icon, result_label = "🎉🥳", "WIN"
                elif event_outcome == "lost":
                    result_icon, result_label = "🥺😔", "LOSS"
                elif event_outcome == "breakeven":
                    result_icon, result_label = "🤝", "BREAK EVEN"
                else:
                    result_icon, result_label = "✅", "CLOSED"

                parts = [
                    provider_line,
                    "",
                    f"{_html(identity.marker)} <b>{_html(identity.reference)} · {tp_label}</b>",
                    f"{result_icon} <b>{result_label}</b>",
                ]
                if event_pnl is not None:
                    parts.extend(["", f"💵 <b>{money(event_pnl)}</b>"])
                if final_settlement and trade is not None:
                    total = trade.realised_pnl
                    total_icon = "🎉🥳" if total > 0 else "🥺😔" if total < 0 else "🤝"
                    total_label = "WIN" if total > 0 else "LOSS" if total < 0 else "BREAK EVEN"
                    parts.extend(
                        [
                            "",
                            f"🏁 <b>TRADE COMPLETE · {total_label}</b>",
                            f"{total_icon} <b>TOTAL {money(total)}</b>",
                        ]
                    )
                if account is not None and account.account_value is not None:
                    parts.extend(
                        ["", f"💰 <b>BALANCE: {_balance_money(account.account_value)}</b>"]
                    )
            elif event_type.startswith("broker_result_"):
                total = trade.realised_pnl if trade is not None else Decimal("0")
                if event_type == "broker_result_win" or total > 0:
                    result_icon, result_label = "🎉🥳", "WIN"
                elif event_type == "broker_result_loss" or total < 0:
                    result_icon, result_label = "🥺😔", "LOSS"
                elif event_type == "broker_result_breakeven" or total == 0:
                    result_icon, result_label = "🤝", "BREAK EVEN"
                else:
                    result_icon, result_label = "✅", "CLOSED"
                parts = [
                    provider_line,
                    "",
                    f"{_html(identity.marker)} <b>{_html(identity.reference)} · TRADE COMPLETE</b>",
                    f"{result_icon} <b>{result_label}</b>",
                ]
                if trade is not None:
                    parts.extend(["", f"💵 <b>TOTAL {money(trade.realised_pnl)}</b>"])
                if account is not None and account.account_value is not None:
                    parts.extend(
                        ["", f"💰 <b>BALANCE: {_balance_money(account.account_value)}</b>"]
                    )
            else:
                update_text = _clean_update_text(row["rendered_text"])
                parts = [
                    provider_line,
                    "",
                    f"{_html(identity.marker)} <b>{_html(identity.reference)} · UPDATE</b>",
                    "",
                    f"🛠 <b>{_html(update_text)}</b>",
                ]
            rendered = "\n".join(parts)
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

    async def _deliver(
        self,
        attempt: PublicationAttempt | SummaryPublicationAttempt,
    ) -> None:
        if isinstance(attempt, SummaryPublicationAttempt):
            await super()._deliver(attempt)
            return

        assert self._bot_token is not None
        assert self._destination_chat_id is not None
        payload: dict[str, Any] = {
            "chat_id": self._destination_chat_id,
            "text": attempt.text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
        if isinstance(attempt, LifecyclePublicationAttempt):
            payload["reply_parameters"] = json.dumps(
                {
                    "message_id": attempt.reply_to_message_id,
                    "allow_sending_without_reply": False,
                }
            )
        try:
            result = await asyncio.to_thread(
                _bot_api_call,
                self._bot_token,
                "sendMessage",
                payload,
            )
            telegram_message_id = int(result["message_id"])
        except (TelegramPublishError, KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, TelegramPublishError):
                code, reason = exc.code, exc.reason
            else:
                code = "telegram_invalid_success_response"
                reason = "Telegram did not return a usable destination message ID."
            await asyncio.to_thread(self._record_failure, attempt, code, reason)
            return

        await asyncio.to_thread(self._record_success, attempt, telegram_message_id)

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
                                s.member_trade_number,
                                s.source_id,
                                COALESCE(NULLIF(src.chat_title,''),src.source_alias,'Unknown provider')
                                    AS provider_name,
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
                            JOIN sources AS src ON src.id=s.source_id
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
                            signal_id,
                            member_trade_number,
                            source_id,
                            provider_name,
                            MAX(symbol) AS symbol,MAX(side) AS side,
                            ARRAY_AGG(DISTINCT tp_index ORDER BY tp_index)
                                FILTER (WHERE effective_status='open') AS open_tp_indices,
                            ARRAY_AGG(DISTINCT tp_index ORDER BY tp_index)
                                FILTER (WHERE effective_status='pending') AS pending_tp_indices
                        FROM leg_state
                        GROUP BY signal_id,member_trade_number,source_id,provider_name
                        HAVING BOOL_OR(effective_status='open') OR BOOL_OR(effective_status='pending')
                        ORDER BY signal_id
                        """
                    )
                ).mappings().all()
            )

    def _render_live_board(self, rows: list[Any]) -> str:
        active_count = len(rows)
        lines = [
            "📌 <b>SUPER SIGNALS · LIVE TRADES</b>",
            f"ACTIVE <b>{active_count}</b>",
        ]

        if self._trade_ledger is not None:
            account = self._trade_ledger.account()
            if account.account_value is not None:
                lines.extend(
                    [
                        "",
                        f"💰 <b>BALANCE {_balance_money(account.account_value)}</b>",
                        (
                            f"Today: <b>{money(account.today_pnl)}</b> · "
                            f"Month: <b>{money(account.month_to_date_pnl)}</b>"
                        ),
                    ]
                )

        if not rows:
            lines.extend(["", "No active trades."])
            return "\n".join(lines)

        for row in rows:
            identity = public_trade_identity(
                row["signal_id"], row["member_trade_number"]
            )
            symbol = str(row["symbol"] or "").upper()
            side = str(row["side"] or "").upper()
            active_indices = sorted(
                {
                    int(value)
                    for value in [
                        *(row["open_tp_indices"] or []),
                        *(row["pending_tp_indices"] or []),
                    ]
                }
            )
            state = (
                "/".join(f"TP{index}" for index in active_indices) + " ACTIVE"
                if active_indices
                else "ACTIVE"
            )
            provider_line = self._provider_line(
                row["source_id"], str(row["provider_name"] or "Unknown provider")
            )
            lines.extend(
                [
                    "",
                    provider_line,
                    (
                        f"<b>{_html(identity.label)}</b> · "
                        f"{_html(symbol)} {_html(side)} · {_html(state)}"
                    ),
                ]
            )
        return "\n".join(lines)



__all__ = ["CanonicalTelegramPublisherManager"]
