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
import logging
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.telegram_publisher import PublicationAttempt, TelegramPublishError, _bot_api_call
from app.telegram_publisher_day20 import LifecyclePublicationAttempt
from app.telegram_publisher_day34 import Day34TelegramPublisherManager, SummaryPublicationAttempt
from app.telegram_publisher_day34_cutover import Day34CutoverTelegramPublisherManager
from app.telegram_trade_ledger import TelegramTradeLedger, TradeLedgerSnapshot, money, provider_badge
from app.trade_identity import public_trade_identity

_PLACEMENT_EVENT = "mt5.day38_route_new_trade"
_MEMBER_EVENT_FRESHNESS = timedelta(minutes=5)
SOFIA = ZoneInfo("Europe/Sofia")
logger = logging.getLogger(__name__)


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
    """Spacious member-facing details for a newly placed trade."""
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

    sections = [
        f"<b>Entry</b>\n{_html(entry_text)}",
        f"<b>Stop Loss</b>\n{_html(_decimal_text(stop_loss))}",
    ]
    for index, target in enumerate(take_profits, start=1):
        sections.append(f"<b>TP{index}</b>\n{_html(_decimal_text(target))}")
    if bool(row["has_open_runner"]):
        sections.append(f"<b>TP{len(take_profits) + 1}</b>\nOPEN")
    return "\n\n".join(sections)


def _absolute_money(value: Decimal | int | float | str | None) -> str:
    amount = Decimal(str(value or 0)).copy_abs().quantize(Decimal("0.01"))
    return "$" + f"{amount:,.2f}"


def _balance_equation(current: Decimal, delta: Decimal) -> str:
    before = (current - delta).quantize(Decimal("0.01"))
    operator = "+" if delta >= 0 else "-"
    return (
        f"{_balance_money(before)} {operator} {_absolute_money(delta)} "
        f"= {_balance_money(current)}"
    )


def _trade_status_lines(trade: TradeLedgerSnapshot | None) -> list[str]:
    if trade is None or not trade.legs:
        return []
    labels = {
        "won": "WON 🥳",
        "closed_profit": "CLOSED IN PROFIT 🥳",
        "lost": "LOST 🥺",
        "breakeven": "BREAK EVEN 🤝",
        "cancelled": "CANCELLED",
        "closed_unknown": "CLOSED ✅",
        "pending": "PENDING ⏳",
    }
    lines = ["<b>Trade Status</b>", ""]
    for leg in trade.legs:
        state = labels.get(leg.status, "PENDING ⏳")
        cash = ""
        if leg.status in {"won", "closed_profit", "lost", "breakeven"}:
            cash = f" · <b>{money(leg.cash_pnl)}</b>"
        elif leg.status == "closed_unknown":
            cash = (
                f" · <b>{money(leg.cash_pnl)}</b>"
                if leg.cash_pnl is not None
                else " · <b>P&L confirming…</b>"
            )
        lines.append(f"TP{leg.tp_index} — <b>{state}</b>{cash}")
    return lines



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
        self._maintenance_task: asyncio.Task[None] | None = None
        self._maintenance_next_at = 0.0

    def _destination_reader_collision(self) -> bool:
        if self._destination_chat_id is None:
            return True
        with self._session_factory() as session:
            return bool(
                session.execute(
                    text(
                        """
                        SELECT 1
                        FROM sources
                        WHERE chat_id=:chat_id
                          AND status IN ('testing','shadow','live')
                        LIMIT 1
                        """
                    ),
                    {"chat_id": self._destination_chat_id},
                ).scalar_one_or_none()
            )

    def _run_member_maintenance_safely(self) -> None:
        """Low-priority Telegram reconciliation that must never block fresh trades."""
        try:
            self._reconcile_financial_state_safely()
            self._repair_sent_root_identities_safely()
            self._repair_sent_trade_messages_safely()
            if self._summary_service is not None:
                self._summary_service.seed_due()
            self._seed_summary_deliveries()
            self._sync_live_board_safely()
        except Exception:
            logger.exception("Telegram member maintenance failed safely; fresh publishing continues")

    async def _run(self) -> None:
        """Publish fresh roots/updates before any slow Telegram reconciliation work."""
        while not self._stop_event.is_set():
            try:
                if self._destination_reader_collision():
                    logger.error("Telegram publisher blocked: destination is also an active reader source")
                else:
                    await asyncio.to_thread(self._seed_missing_publications)
                    # Drain a small burst immediately. A new trade must not sit behind
                    # a 3-second sleep for every older update in the queue.
                    for _ in range(20):
                        attempt = await asyncio.to_thread(self._claim_next)
                        if attempt is None:
                            break
                        await self._deliver(attempt)

                loop = asyncio.get_running_loop()
                now = loop.time()
                if (
                    now >= self._maintenance_next_at
                    and (self._maintenance_task is None or self._maintenance_task.done())
                ):
                    if self._maintenance_task is not None and self._maintenance_task.done():
                        try:
                            self._maintenance_task.result()
                        except Exception:
                            logger.exception("Telegram maintenance task failed safely")
                    self._maintenance_task = asyncio.create_task(
                        asyncio.to_thread(self._run_member_maintenance_safely),
                        name="telegram-publisher-maintenance",
                    )
                    self._maintenance_next_at = now + 15.0
            except Exception:
                logger.exception("Telegram fast publication loop failed safely; trading unchanged")

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                continue

    def _account_lines(self, *, include_origin: bool = False) -> list[str]:
        if self._trade_ledger is None:
            return []
        return self._trade_ledger.account_lines(
            self._trade_ledger.account(),
            include_origin=include_origin,
        )

    def _financial_business_date(self) -> Any:
        return datetime.now(UTC).astimezone(SOFIA).date()

    def _ensure_financial_state(self, session: Any) -> Any:
        """Return today's locked Telegram money state, creating it when needed.

        This state is deliberately independent from floating MT5 equity. The member
        feed is a closed-balance ledger: only realised, published broker settlements
        move Balance and Today's P&L.
        """
        if self._reference_user_id is None or self._destination_chat_id is None:
            return None

        business_date = self._financial_business_date()
        row = session.execute(
            text(
                """
                SELECT reference_user_id,destination_chat_id,business_date,
                       balance,daily_pnl,last_publication_id
                FROM telegram_financial_state
                WHERE reference_user_id=:user_id
                  AND destination_chat_id=:chat_id
                  AND business_date=:business_date
                FOR UPDATE
                """
            ),
            {
                "user_id": self._reference_user_id,
                "chat_id": self._destination_chat_id,
                "business_date": business_date,
            },
        ).mappings().first()
        if row is not None:
            return row

        previous = session.execute(
            text(
                """
                SELECT balance
                FROM telegram_financial_state
                WHERE reference_user_id=:user_id
                  AND destination_chat_id=:chat_id
                  AND business_date<:business_date
                ORDER BY business_date DESC
                LIMIT 1
                """
            ),
            {
                "user_id": self._reference_user_id,
                "chat_id": self._destination_chat_id,
                "business_date": business_date,
            },
        ).mappings().first()

        if previous is not None:
            opening_balance = Decimal(str(previous["balance"])).quantize(Decimal("0.01"))
        else:
            snapshot = session.execute(
                text(
                    """
                    SELECT pas.balance
                    FROM performance_account_snapshots pas
                    JOIN mt5_accounts a ON a.id=pas.mt5_account_id
                    WHERE a.owner_user_id=:user_id
                      AND a.status<>'revoked'
                      AND pas.balance IS NOT NULL
                    ORDER BY pas.captured_at DESC
                    LIMIT 1
                    """
                ),
                {"user_id": self._reference_user_id},
            ).mappings().first()
            opening_balance = Decimal(str(snapshot["balance"] if snapshot else 0)).quantize(
                Decimal("0.01")
            )

        session.execute(
            text(
                """
                INSERT INTO telegram_financial_state(
                    reference_user_id,destination_chat_id,business_date,
                    balance,daily_pnl,last_publication_id,created_at,updated_at
                )
                VALUES(
                    :user_id,:chat_id,:business_date,
                    :balance,0,NULL,now(),now()
                )
                ON CONFLICT(reference_user_id,destination_chat_id,business_date)
                DO NOTHING
                """
            ),
            {
                "user_id": self._reference_user_id,
                "chat_id": self._destination_chat_id,
                "business_date": business_date,
                "balance": opening_balance,
            },
        )
        return session.execute(
            text(
                """
                SELECT reference_user_id,destination_chat_id,business_date,
                       balance,daily_pnl,last_publication_id
                FROM telegram_financial_state
                WHERE reference_user_id=:user_id
                  AND destination_chat_id=:chat_id
                  AND business_date=:business_date
                FOR UPDATE
                """
            ),
            {
                "user_id": self._reference_user_id,
                "chat_id": self._destination_chat_id,
                "business_date": business_date,
            },
        ).mappings().one()

    def _reserve_financial_publication(
        self,
        session: Any,
        publication_id: UUID,
        event_delta: Decimal | int | float | str | None,
    ) -> tuple[Decimal, Decimal]:
        """Reserve deterministic Balance/P&L values for one Telegram publication.

        Retrying the same publication reuses the same arithmetic. The durable rolling
        state advances only after Telegram confirms delivery.
        """
        existing = session.execute(
            text(
                """
                SELECT new_balance,new_daily_pnl
                FROM telegram_publication_financials
                WHERE publication_id=:publication_id
                """
            ),
            {"publication_id": publication_id},
        ).mappings().first()
        if existing is not None:
            return (
                Decimal(str(existing["new_balance"])).quantize(Decimal("0.01")),
                Decimal(str(existing["new_daily_pnl"])).quantize(Decimal("0.01")),
            )

        state = self._ensure_financial_state(session)
        if state is None:
            return Decimal("0.00"), Decimal("0.00")

        delta = Decimal(str(event_delta or 0)).quantize(Decimal("0.01"))
        prior_balance = Decimal(str(state["balance"])).quantize(Decimal("0.01"))
        prior_daily = Decimal(str(state["daily_pnl"])).quantize(Decimal("0.01"))
        new_balance = (prior_balance + delta).quantize(Decimal("0.01"))
        new_daily = (prior_daily + delta).quantize(Decimal("0.01"))

        session.execute(
            text(
                """
                INSERT INTO telegram_publication_financials(
                    publication_id,reference_user_id,destination_chat_id,business_date,
                    event_delta,prior_balance,prior_daily_pnl,new_balance,new_daily_pnl,
                    status,created_at,updated_at
                )
                VALUES(
                    :publication_id,:user_id,:chat_id,:business_date,
                    :delta,:prior_balance,:prior_daily,:new_balance,:new_daily,
                    'reserved',now(),now()
                )
                ON CONFLICT(publication_id) DO NOTHING
                """
            ),
            {
                "publication_id": publication_id,
                "user_id": self._reference_user_id,
                "chat_id": self._destination_chat_id,
                "business_date": state["business_date"],
                "delta": delta,
                "prior_balance": prior_balance,
                "prior_daily": prior_daily,
                "new_balance": new_balance,
                "new_daily": new_daily,
            },
        )
        return new_balance, new_daily

    @staticmethod
    def _financial_lines(balance: Decimal, daily_pnl: Decimal) -> list[str]:
        return [
            "",
            "<b>Balance</b>",
            f"<b>{_balance_money(balance)}</b>",
            "",
            "<b>Today’s P&L</b>",
            f"<b>{money(daily_pnl)}</b>",
        ]

    def _commit_financial_publication(self, publication_id: UUID) -> None:
        if self._reference_user_id is None or self._destination_chat_id is None:
            return
        with self._session_factory() as session:
            fin = session.execute(
                text(
                    """
                    SELECT f.business_date,f.new_balance,f.new_daily_pnl,p.sent_at
                    FROM telegram_publication_financials f
                    JOIN telegram_publications p ON p.id=f.publication_id
                    WHERE f.publication_id=:publication_id
                      AND p.status='sent'
                    FOR UPDATE OF f
                    """
                ),
                {"publication_id": publication_id},
            ).mappings().first()
            if fin is None:
                session.rollback()
                return

            session.execute(
                text(
                    """
                    UPDATE telegram_financial_state
                    SET balance=:balance,
                        daily_pnl=:daily_pnl,
                        last_publication_id=:publication_id,
                        updated_at=now()
                    WHERE reference_user_id=:user_id
                      AND destination_chat_id=:chat_id
                      AND business_date=:business_date
                    """
                ),
                {
                    "balance": fin["new_balance"],
                    "daily_pnl": fin["new_daily_pnl"],
                    "publication_id": publication_id,
                    "user_id": self._reference_user_id,
                    "chat_id": self._destination_chat_id,
                    "business_date": fin["business_date"],
                },
            )
            session.execute(
                text(
                    """
                    UPDATE telegram_publication_financials
                    SET status='sent',sent_at=:sent_at,updated_at=now()
                    WHERE publication_id=:publication_id
                    """
                ),
                {"publication_id": publication_id, "sent_at": fin["sent_at"]},
            )
            session.commit()

    def _fail_financial_publication(self, publication_id: UUID) -> None:
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE telegram_publication_financials
                    SET status='failed',updated_at=now()
                    WHERE publication_id=:publication_id
                      AND status='reserved'
                    """
                ),
                {"publication_id": publication_id},
            )
            session.commit()

    def _reconcile_financial_state_safely(self) -> None:
        """Recover the tiny crash window between Telegram send and ledger commit."""
        if self._reference_user_id is None or self._destination_chat_id is None:
            return
        try:
            with self._session_factory() as session:
                rows = session.execute(
                    text(
                        """
                        SELECT f.publication_id
                        FROM telegram_publication_financials f
                        JOIN telegram_publications p ON p.id=f.publication_id
                        WHERE f.reference_user_id=:user_id
                          AND f.destination_chat_id=:chat_id
                          AND f.status='reserved'
                          AND p.status='sent'
                          AND p.sent_at IS NOT NULL
                        ORDER BY p.sent_at,p.id
                        LIMIT 100
                        """
                    ),
                    {
                        "user_id": self._reference_user_id,
                        "chat_id": self._destination_chat_id,
                    },
                ).scalars().all()
            for publication_id in rows:
                self._commit_financial_publication(publication_id)
        except Exception:
            logger.exception("Telegram rolling financial ledger reconciliation failed safely")

    @staticmethod
    def _provider_line(source_id: UUID | str | None, provider_name: str) -> str:
        return f"{provider_badge(source_id, provider_name)} <b>{_html(provider_name)}</b>"

    def _trade_status_lines_as_of(
        self,
        session: Any,
        signal_id: UUID,
        event_at: datetime,
    ) -> list[str]:
        """Render the trade exactly as it stood at this event time.

        Historical member posts must never borrow statuses from later broker events.
        """
        rows = session.execute(
            text(
                """
                SELECT
                    p.tp_index,
                    p.status AS position_status,
                    p.closed_at,
                    p.pnl_amount,
                    o.status AS outcome_status,
                    o.cash_pnl
                FROM positions p
                LEFT JOIN performance_trade_outcomes o
                  ON o.position_id=p.id
                 AND o.user_id=:user_id
                WHERE p.signal_id=:signal_id
                ORDER BY p.tp_index,p.id
                """
            ),
            {
                "signal_id": signal_id,
                "user_id": self._reference_user_id,
            },
        ).mappings().all()
        if not rows:
            return []

        labels = {
            "won": "WON 🥳",
            "closed_profit": "CLOSED IN PROFIT 🥳",
            "lost": "LOST 🥺",
            "breakeven": "BREAK EVEN 🤝",
            "cancelled": "CANCELLED",
            "closed_unknown": "CLOSED ✅",
            "pending": "PENDING ⏳",
        }
        lines = ["<b>Trade Status</b>", ""]
        for row in rows:
            closed_at = row["closed_at"]
            if closed_at is None or closed_at > event_at:
                status = "pending"
                cash = None
            else:
                status = str(row["outcome_status"] or "").lower()
                cash = row["cash_pnl"]
                if not status:
                    value = row["pnl_amount"]
                    if value is None:
                        status = "closed_unknown"
                    else:
                        amount = Decimal(str(value))
                        if amount > 0:
                            status = "closed_profit"
                        elif amount < 0:
                            status = "lost"
                        else:
                            status = "breakeven"
                    cash = value

            state = labels.get(status, "PENDING ⏳")
            suffix = ""
            if status in {"won", "closed_profit", "lost", "breakeven"}:
                suffix = f" · <b>{money(cash)}</b>"
            elif status == "closed_unknown":
                suffix = (
                    f" · <b>{money(cash)}</b>"
                    if cash is not None
                    else " · <b>P&L confirming…</b>"
                )
            lines.append(f"TP{int(row['tp_index'])} — <b>{state}</b>{suffix}")
        return lines

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

    @staticmethod
    def _management_broker_action_sql(alias: str) -> str:
        return f"""
            EXISTS (
                SELECT 1
                FROM audit_events AS routed
                WHERE routed.event_type='mt5.day28_route_success'
                  AND routed.payload->>'route'='trade_update'
                  AND routed.payload->>'lifecycle_event_id'={alias}.id::text
                  AND COALESCE((routed.payload->>'broker_actions_sent')::int,0) > 0
            )
        """

    @staticmethod
    def _queued_from_confirmed_placement_sql(alias: str) -> str:
        """A root seeded promptly from a broker-confirmed route stays sendable.

        Freshness decides whether a root may be created. Once a publication row was
        legitimately created within the freshness window of the executed broker route,
        publisher delay/rate-limit/restart must not age that member post out.
        """
        return f"""
            EXISTS (
                SELECT 1
                FROM audit_events AS placed
                WHERE placed.entity_type='signal'
                  AND placed.entity_id={alias}.signal_id
                  AND placed.event_type='{_PLACEMENT_EVENT}'
                  AND placed.payload->>'outcome'='executed'
                  AND {alias}.created_at>=placed.created_at
                  AND {alias}.created_at<=placed.created_at+INTERVAL '5 minutes'
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
            management_action_for_ev = self._management_broker_action_sql("ev")
            queued_from_confirmed_route = self._queued_from_confirmed_placement_sql("pub")

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
                      AND NOT (
                          {queued_from_confirmed_route}
                          AND pub.created_at>now()-INTERVAL '60 minutes'
                      )
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
                      AND (
                          {placement_for_pub}
                          OR (
                              {queued_from_confirmed_route}
                              AND pub.created_at>now()-INTERVAL '60 minutes'
                          )
                      )
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

            # Aggregate broker_result_* events are durable audit evidence only.
            # Position-level broker settlements are the one canonical member result path.
            session.execute(
                text(
                    """
                    UPDATE telegram_publications AS pub
                    SET status='suppressed',
                        failure_code='aggregate_broker_result_audit_only',
                        failure_reason='Aggregate broker result is audit-only; member feed uses position settlements.',
                        updated_at=now()
                    FROM signal_lifecycle_events AS ev
                    WHERE pub.lifecycle_event_id=ev.id
                      AND pub.status IN ('pending','sending')
                      AND ev.event_type LIKE 'broker_result_%'
                    """
                )
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
                      AND (
                          ev.origin<>'provider_update'
                          OR {management_action_for_ev}
                      )
                      AND ev.event_type NOT LIKE 'broker_result_%'
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

    def _repair_sent_root_identities_safely(self) -> None:
        """Replace any leaked internal SS-* root with its assigned member TRADE N.

        Roots are now blocked until member_trade_number exists, but this also repairs
        the handful of rows that escaped during the old assignment/send race.
        """
        if not self._bot_token or self._destination_chat_id is None:
            return
        try:
            with self._session_factory() as session:
                rows = session.execute(
                    text(
                        """
                        SELECT
                            pub.id AS publication_id,
                            pub.signal_id,
                            pub.telegram_message_id,
                            COALESCE(pub.destination_chat_id,:destination_chat_id)
                                AS destination_chat_id,
                            pub.rendered_text,
                            pub.created_at AS root_created_at,
                            sig.member_trade_number
                        FROM telegram_publications AS pub
                        JOIN signals AS sig ON sig.id=pub.signal_id
                        WHERE pub.publication_kind='signal_created'
                          AND pub.lifecycle_event_id IS NULL
                          AND pub.status='sent'
                          AND pub.telegram_message_id IS NOT NULL
                          AND sig.member_trade_number IS NOT NULL
                          AND pub.created_at>=now()-INTERVAL '24 hours'
                          AND COALESCE(pub.failure_code,'') NOT IN (
                              'sent_root_uneditable_truth_corrected',
                              'sent_root_uneditable_no_correction'
                          )
                        ORDER BY pub.updated_at DESC
                        LIMIT 100
                        """
                    ),
                    {"destination_chat_id": self._destination_chat_id},
                ).mappings().all()

            for row in rows:
                rendered = str(row["rendered_text"] or "")
                repaired = re.sub(
                    r"SS-[0-9A-F]{10}",
                    f"TRADE {int(row['member_trade_number'])}",
                    rendered,
                    count=1,
                )
                # A NEW TRADE post is an immutable entry snapshot. Do not rewrite
                # historical root status as later TP/SL events arrive; those belong in
                # chronological lifecycle replies below the root.
                if repaired == rendered:
                    continue
                try:
                    _bot_api_call(
                        self._bot_token,
                        "editMessageText",
                        {
                            "chat_id": int(row["destination_chat_id"]),
                            "message_id": int(row["telegram_message_id"]),
                            "text": repaired,
                            "parse_mode": "HTML",
                            "disable_web_page_preview": "true",
                        },
                    )
                except TelegramPublishError as exc:
                    reason = exc.reason.lower()
                    if "message to edit not found" not in reason:
                        logger.warning(
                            "Telegram root trade repair failed safely message_id=%s code=%s reason=%s",
                            row["telegram_message_id"],
                            exc.code,
                            exc.reason,
                        )
                        continue

                    # Never create a second trade-status post because an old root is
                    # uneditable. Mark the identity repair as handled and leave the
                    # chronological lifecycle stream untouched.
                    correction_message_id: int | None = None
                    marker = "sent_root_uneditable_no_correction"
                    marker_reason = (
                        "Telegram could not edit this historical root identity; no replacement "
                        "was posted to avoid duplicate/out-of-sequence messages."
                    )

                    with self._session_factory() as session:
                        session.execute(
                            text(
                                """
                                UPDATE telegram_publications
                                SET failure_code=:failure_code,
                                    failure_reason=:failure_reason,
                                    updated_at=now()
                                WHERE id=:publication_id AND status='sent'
                                """
                            ),
                            {
                                "publication_id": row["publication_id"],
                                "failure_code": marker,
                                "failure_reason": marker_reason,
                            },
                        )
                        session.commit()

                    logger.info(
                        "Telegram uneditable root handled once message_id=%s correction_message_id=%s",
                        row["telegram_message_id"],
                        correction_message_id,
                    )
                    continue

                with self._session_factory() as session:
                    session.execute(
                        text(
                            """
                            UPDATE telegram_publications
                            SET rendered_text=:rendered_text,updated_at=now()
                            WHERE id=:publication_id AND status='sent'
                            """
                        ),
                        {
                            "publication_id": row["publication_id"],
                            "rendered_text": repaired,
                        },
                    )
                    session.commit()
                logger.info(
                    "Telegram root trade snapshot corrected in place message_id=%s trade_number=%s",
                    row["telegram_message_id"],
                    row["member_trade_number"],
                )
        except Exception:
            logger.exception("Telegram root identity repair failed safely; trading unchanged")

    def _repair_sent_trade_messages_safely(self) -> None:
        """Edit stale member updates in place; never create another Telegram post.

        Two repairs are allowed:
        * the latest sent lifecycle message for a signal may refresh its Trade Status
          snapshot after broker/local reconciliation;
        * an older partial-management message may have its action wording corrected when
          the original provider message explicitly named a different TP milestone.

        Telegram edits are idempotent and failures never affect trading.
        """
        if (
            self._trade_ledger is None
            or not self._bot_token
            or self._destination_chat_id is None
        ):
            return
        try:
            with self._session_factory() as session:
                rows = session.execute(
                    text(
                        """
                        SELECT
                            pub.id AS publication_id,
                            pub.signal_id,
                            pub.telegram_message_id,
                            pub.destination_chat_id,
                            pub.reply_to_telegram_message_id,
                            pub.rendered_text,
                            ev.event_type,
                            ev.aggregate_result,
                            ev.occurred_at AS repair_event_occurred_at,
                            event_position.tp_index AS event_tp_index,
                            COALESCE(event_outcome.status,'') AS event_outcome_status,
                            event_outcome.cash_pnl AS repair_event_cash_pnl,
                            fin.new_balance AS repair_financial_balance,
                            fin.new_daily_pnl AS repair_financial_daily_pnl,
                            COALESCE(m.raw_text,'') AS source_raw_text,
                            NOT EXISTS (
                                SELECT 1
                                FROM telegram_publications newer
                                WHERE newer.signal_id=pub.signal_id
                                  AND newer.publication_kind='lifecycle_event'
                                  AND newer.status='sent'
                                  AND newer.telegram_message_id IS NOT NULL
                                  AND (
                                      newer.created_at>pub.created_at
                                      OR (
                                          newer.created_at=pub.created_at
                                          AND newer.id>pub.id
                                      )
                                  )
                            ) AS is_latest,
                            EXISTS (
                                SELECT 1
                                FROM telegram_publications newer
                                JOIN signal_lifecycle_events newer_ev
                                  ON newer_ev.id=newer.lifecycle_event_id
                                WHERE newer.signal_id=pub.signal_id
                                  AND newer.publication_kind='lifecycle_event'
                                  AND newer.status='sent'
                                  AND newer.telegram_message_id IS NOT NULL
                                  AND newer_ev.event_type='broker_position_settled'
                                  AND ev.event_type='broker_position_settled'
                                  AND ABS(EXTRACT(EPOCH FROM (
                                      newer_ev.occurred_at-ev.occurred_at
                                  )))<=30
                                  AND (
                                      newer_ev.occurred_at>ev.occurred_at
                                      OR (
                                          newer_ev.occurred_at=ev.occurred_at
                                          AND newer.id>pub.id
                                      )
                                  )
                            ) AS has_newer_same_burst
                        FROM telegram_publications pub
                        JOIN signal_lifecycle_events ev ON ev.id=pub.lifecycle_event_id
                        LEFT JOIN messages m ON m.id=ev.source_message_id
                        LEFT JOIN positions event_position
                          ON event_position.id::text=COALESCE(ev.aggregate_result->>'position_id','')
                        LEFT JOIN performance_trade_outcomes event_outcome
                          ON event_outcome.position_id=event_position.id
                         AND event_outcome.user_id=:reference_user_id
                        LEFT JOIN telegram_publication_financials fin
                          ON fin.publication_id=pub.id
                         AND fin.status='sent'
                        WHERE pub.status='sent'
                          AND pub.publication_kind='lifecycle_event'
                          AND pub.telegram_message_id IS NOT NULL
                          AND pub.created_at>=now()-INTERVAL '24 hours'
                        ORDER BY pub.created_at DESC
                        LIMIT 100
                        """
                    ),
                    {"reference_user_id": self._reference_user_id},
                ).mappings().all()

            for row in rows:
                chat_id = (
                    int(row["destination_chat_id"])
                    if row["destination_chat_id"] is not None
                    else int(self._destination_chat_id)
                )
                message_id = int(row["telegram_message_id"])

                original = str(row["rendered_text"] or "")
                if not original:
                    continue
                repaired = original

                # Financial lines are immutable rolling-ledger values. Never rebuild
                # them from historical equity or an event-time account snapshot.
                repair_balance = row["repair_financial_balance"]
                repair_daily = row["repair_financial_daily_pnl"]
                if repair_balance is not None and repair_daily is not None:
                    repaired = re.sub(
                        r"(<b>Balance</b>\n)<b>[^\n]+</b>",
                        rf"\1<b>{_balance_money(repair_balance)}</b>",
                        repaired,
                        count=1,
                    )
                    repaired = re.sub(
                        r"(<b>Today’s P&L</b>\n)<b>[^\n]+</b>",
                        rf"\1<b>{money(repair_daily)}</b>",
                        repaired,
                        count=1,
                    )

                # Historical TIG-style partial wording: the original source explicitly
                # names the target milestone. Never leave a sent post claiming TP1 when
                # the provider actually said TP2/TP3/etc.
                source_raw = str(row["source_raw_text"] or "")
                milestone = re.search(
                    r"\bTP\s*(\d+)\s*(?:HIT|HITS|REACHED|TAPPED)\b",
                    source_raw,
                    re.IGNORECASE,
                )
                partial = re.search(
                    r"\b(?:BOOK|TAKE|CLOSE|BANK|SECURE)\b[^\n]{0,35}"
                    r"\b(?:PARTIALS?|HALF|SOME\s+PROFIT|PROFIT\s+OFF)\b",
                    source_raw,
                    re.IGNORECASE,
                )
                if milestone is not None and partial is not None:
                    target = int(milestone.group(1))
                    repaired = re.sub(
                        r"Book partial profit at TP1\.",
                        f"TP{target} hit. Book partial profit.",
                        repaired,
                        count=1,
                    )

                if repaired == original:
                    continue

                replacement_message_id = message_id
                try:
                    _bot_api_call(
                        self._bot_token,
                        "editMessageText",
                        {
                            "chat_id": chat_id,
                            "message_id": message_id,
                            "text": repaired,
                            "parse_mode": "HTML",
                            "disable_web_page_preview": "true",
                        },
                    )
                except TelegramPublishError as exc:
                    reason = exc.reason.lower()
                    if "message is not modified" in reason:
                        pass
                    elif "message to edit not found" in reason:
                        with self._session_factory() as session:
                            session.execute(
                                text(
                                    """
                                    UPDATE telegram_publications
                                    SET failure_code='sent_lifecycle_uneditable_no_replacement',
                                        failure_reason='Historical Telegram update is uneditable; no replacement was sent.',
                                        updated_at=now()
                                    WHERE id=:publication_id AND status='sent'
                                    """
                                ),
                                {"publication_id": row["publication_id"]},
                            )
                            session.commit()
                        continue
                    else:
                        raise

                with self._session_factory() as session:
                    session.execute(
                        text(
                            """
                            UPDATE telegram_publications
                            SET rendered_text=:rendered_text,
                                telegram_message_id=:telegram_message_id,
                                updated_at=now()
                            WHERE id=:publication_id AND status='sent'
                            """
                        ),
                        {
                            "publication_id": row["publication_id"],
                            "rendered_text": repaired,
                            "telegram_message_id": replacement_message_id,
                        },
                    )
                    session.commit()
                logger.info(
                    "Telegram trade message corrected in place signal=%s message_id=%s",
                    row["signal_id"],
                    message_id,
                )
        except Exception:
            logger.exception("Telegram sent-trade truth repair failed safely; trading unchanged")

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
                    'NEW TRADE PLACED — ' || COALESCE('TRADE ' || sig.member_trade_number::text,'TRADE'),
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
                    COALESCE('TRADE ' || sig.member_trade_number::text,'TRADE') || ' · ' ||
                    CASE
                        WHEN ev.event_type='broker_result_win' THEN 'CLOSED — WIN'
                        WHEN ev.event_type='broker_result_loss' THEN 'CLOSED — LOSS'
                        WHEN ev.event_type='broker_result_breakeven' THEN 'CLOSED — BREAK EVEN'
                        WHEN ev.event_type='broker_result_closed' THEN 'CLOSED'
                        ELSE 'UPDATE'
                    END,
                    COALESCE('TRADE ' || sig.member_trade_number::text,'TRADE') || ' · ' || ev.rendered_text,
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

    def _next_member_publication_kind(self) -> str | None:
        """Choose the earliest real member event across roots and lifecycle updates."""
        fresh_after = datetime.now(UTC) - _MEMBER_EVENT_FRESHNESS
        placement_for_pub = self._placement_exists_sql("pub.signal_id")
        queued_from_confirmed_route = self._queued_from_confirmed_placement_sql("pub")
        with self._session_factory() as session:
            return session.execute(
                text(
                    f"""
                    WITH candidates AS (
                        SELECT
                            'root'::text AS kind,
                            pub.created_at AS event_at,
                            pub.id
                        FROM telegram_publications pub
                        JOIN signals sig ON sig.id=pub.signal_id
                        WHERE pub.status='pending'
                          AND pub.publication_kind='signal_created'
                          AND pub.lifecycle_event_id IS NULL
                          AND sig.member_trade_number IS NOT NULL
                          AND (
                              {placement_for_pub}
                              OR (
                                  {queued_from_confirmed_route}
                                  AND pub.created_at>now()-INTERVAL '60 minutes'
                              )
                          )

                        UNION ALL

                        SELECT
                            'lifecycle'::text AS kind,
                            ev.occurred_at AS event_at,
                            pub.id
                        FROM telegram_publications pub
                        JOIN signal_lifecycle_events ev ON ev.id=pub.lifecycle_event_id
                        JOIN telegram_publications root
                          ON root.signal_id=pub.signal_id
                         AND root.publication_kind='signal_created'
                         AND root.lifecycle_event_id IS NULL
                        WHERE pub.status='pending'
                          AND pub.publication_kind='lifecycle_event'
                          AND root.status='sent'
                          AND root.telegram_message_id IS NOT NULL
                          AND ev.created_at>=:fresh_after
                    )
                    SELECT kind
                    FROM candidates
                    ORDER BY event_at,id
                    LIMIT 1
                    """
                ),
                {"fresh_after": fresh_after},
            ).scalar_one_or_none()

    def _claim_next(self) -> PublicationAttempt | SummaryPublicationAttempt | None:
        kind = self._next_member_publication_kind()
        if kind == "lifecycle":
            lifecycle = self._claim_lifecycle()
            if lifecycle is not None:
                return lifecycle
            root = self._claim_root()
            if root is not None:
                return root
        else:
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
                      AND sig.member_trade_number IS NOT NULL
                      AND (
                          {self._placement_exists_sql('pub.signal_id')}
                          OR (
                              {self._queued_from_confirmed_placement_sql('pub')}
                              AND pub.created_at>now()-INTERVAL '60 minutes'
                          )
                      )
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
            instrument = "GOLD" if symbol.startswith("XAU") else symbol
            side_icon = "🟢" if side == "BUY" else "🔴" if side == "SELL" else "⚪"
            parts = [
                provider_line,
                "",
                f"<b>{_html(identity.reference)} · NEW TRADE</b>",
                "",
                f"{side_icon} <b>{_html(side)} {_html(instrument)}</b>",
                "",
                _render_root(row),
            ]
            if self._reference_user_id is not None:
                rolling_balance, rolling_daily = self._reserve_financial_publication(
                    session,
                    row["publication_id"],
                    Decimal("0"),
                )
                parts.extend(self._financial_lines(rolling_balance, rolling_daily))
            if self._trade_ledger is not None:
                trade = self._trade_ledger.trade(row["signal_id"])
                if trade is not None and trade.legs:
                    parts.extend(["", "<b>Trade Status</b>", ""])
                    for leg in trade.legs:
                        parts.append(f"TP{leg.tp_index} — <b>PENDING ⏳</b>")
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
                        ev.event_type,ev.rendered_text,ev.aggregate_result,ev.occurred_at AS event_occurred_at,
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
                      AND ev.event_type NOT LIKE 'broker_result_%'
                      AND NOT (
                          ev.event_type='broker_position_settled'
                          AND EXISTS (
                              SELECT 1
                              FROM positions earlier
                              WHERE earlier.signal_id=ev.signal_id
                                AND earlier.closed_at IS NOT NULL
                                AND (
                                    earlier.closed_at<ev.occurred_at
                                    OR (
                                        earlier.closed_at=ev.occurred_at
                                        AND event_position.tp_index IS NOT NULL
                                        AND earlier.tp_index<event_position.tp_index
                                    )
                                )
                                AND NOT EXISTS (
                                    SELECT 1
                                    FROM signal_lifecycle_events earlier_ev
                                    WHERE earlier_ev.signal_id=ev.signal_id
                                      AND earlier_ev.event_type='broker_position_settled'
                                      AND COALESCE(earlier_ev.aggregate_result->>'position_id','')=earlier.id::text
                                )
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
            matching_leg = next(
                (
                    leg
                    for leg in (trade.legs if trade is not None else ())
                    if leg.tp_index == int(tp_index or 1)
                ),
                None,
            )
            if event_pnl is None and matching_leg is not None:
                event_pnl = matching_leg.cash_pnl
            if not event_outcome and matching_leg is not None:
                if matching_leg.status in {"won", "closed_profit"}:
                    event_outcome = "won"
                elif matching_leg.status == "lost":
                    event_outcome = "lost"
                elif matching_leg.status == "breakeven":
                    event_outcome = "breakeven"
                elif matching_leg.status == "closed_unknown":
                    event_outcome = "closed_unknown"

            # Never publish a realised-result post until its broker cash is known.
            # Otherwise a later repair would have to rewrite the rolling ledger and
            # every subsequent post. Waiting preserves exact once-only arithmetic.
            if event_type == "broker_position_settled" and event_pnl is None:
                session.rollback()
                return None

            financial_delta = (
                Decimal(str(event_pnl)).quantize(Decimal("0.01"))
                if event_type == "broker_position_settled" and event_pnl is not None
                else Decimal("0.00")
            )
            rolling_balance, rolling_daily = self._reserve_financial_publication(
                session,
                row["publication_id"],
                financial_delta,
            )

            has_later_closed_leg = bool(
                session.execute(
                    text(
                        """
                        SELECT 1
                        FROM positions later
                        WHERE later.signal_id=:signal_id
                          AND later.closed_at IS NOT NULL
                          AND (
                              later.closed_at>:event_at
                              OR (
                                  later.closed_at=:event_at
                                  AND :event_tp_index IS NOT NULL
                                  AND later.tp_index>:event_tp_index
                              )
                          )
                        LIMIT 1
                        """
                    ),
                    {
                        "signal_id": row["signal_id"],
                        "event_at": row["event_occurred_at"],
                        "event_tp_index": tp_index,
                    },
                ).scalar_one_or_none()
            )
            trade_pnl_confirmed = bool(
                trade is not None
                and all(
                    leg.cash_pnl is not None
                    for leg in trade.legs
                    if leg.status in {
                        "won",
                        "closed_profit",
                        "lost",
                        "breakeven",
                        "closed_unknown",
                    }
                )
            )
            final_settlement = (
                event_type == "broker_position_settled"
                and trade is not None
                and trade.complete
                and trade_pnl_confirmed
                and not has_later_closed_leg
            )

            if event_type == "broker_position_settled":
                pnl = (
                    Decimal(str(event_pnl)).quantize(Decimal("0.01"))
                    if event_pnl is not None
                    else None
                )
                if event_pnl is None:
                    heading = f"<b>TRADE UPDATE 📈 · {tp_label} CLOSED</b>"
                    result = "✅ <b>P&L confirming…</b>"
                elif final_settlement and trade is not None:
                    total = trade.realised_pnl.quantize(Decimal("0.01"))
                    if total < 0:
                        heading = "<b>TRADE UPDATE 📉 · TRADE COMPLETE</b>"
                        result = f"🥺😔 <b>TRADE LOSS · {money(total)}</b>"
                    elif total > 0:
                        heading = "<b>TRADE UPDATE 📈 · TRADE COMPLETE</b>"
                        result = f"🎉🥳 <b>TRADE WIN · {money(total)}</b>"
                    else:
                        heading = "<b>TRADE UPDATE 📈 · TRADE COMPLETE</b>"
                        result = "🤝 <b>BREAK EVEN</b>"
                elif event_outcome == "won":
                    leg_state = next(
                        (
                            leg.status
                            for leg in (trade.legs if trade is not None else ())
                            if leg.tp_index == int(tp_index or 1)
                        ),
                        None,
                    )
                    if leg_state == "won":
                        heading = f"<b>TRADE UPDATE 📈 · {tp_label} HIT</b>"
                    else:
                        heading = f"<b>TRADE UPDATE 📈 · {tp_label} CLOSED IN PROFIT</b>"
                    result = f"🎉🥳 <b>{money(pnl)} PROFIT</b>"
                elif event_outcome == "lost":
                    heading = "<b>TRADE UPDATE 📉 · STOP LOSS HIT</b>"
                    result = f"🥺😔 <b>{money(pnl)} LOSS</b>"
                elif event_outcome == "breakeven":
                    heading = "<b>TRADE UPDATE 📈 · BREAK EVEN</b>"
                    result = "🤝 <b>$0.00</b>"
                else:
                    heading = f"<b>TRADE UPDATE 📈 · {tp_label} CLOSED</b>"
                    result = f"✅ <b>{money(pnl)}</b>"

                parts = [provider_line, "", heading, "", result]
                parts.extend(self._financial_lines(rolling_balance, rolling_daily))
                status_lines = self._trade_status_lines_as_of(
                    session,
                    row["signal_id"],
                    row["event_occurred_at"],
                )
                if status_lines:
                    parts.extend(["", *status_lines])
                if final_settlement:
                    parts.extend(["", "🏁 <b>Trade complete</b>"])

            else:
                update_text = _clean_update_text(row["rendered_text"])
                parts = [
                    provider_line,
                    "",
                    "<b>TRADE UPDATE 📈</b>",
                    "",
                    f"🛠 <b>{_html(update_text)}</b>",
                ]
                parts.extend(self._financial_lines(rolling_balance, rolling_daily))
                status_lines = _trade_status_lines(trade)
                if status_lines:
                    parts.extend(["", *status_lines])
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
            await asyncio.to_thread(
                self._fail_financial_publication,
                attempt.publication_id,
            )
            return

        await asyncio.to_thread(self._record_success, attempt, telegram_message_id)
        await asyncio.to_thread(
            self._commit_financial_publication,
            attempt.publication_id,
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
                "/".join(f"TP{index}" for index in active_indices) + " PENDING"
                if active_indices
                else "PENDING"
            )
            provider_line = self._provider_line(
                row["source_id"], str(row["provider_name"] or "Unknown provider")
            )
            side_icon = "🟢" if side == "BUY" else "🔴" if side == "SELL" else "⚪"
            instrument = "GOLD" if symbol.startswith("XAU") else symbol
            lines.extend(
                [
                    "",
                    provider_line,
                    (
                        f"<b>{_html(identity.reference)}</b> · "
                        f"{side_icon} <b>{_html(side)} {_html(instrument)}</b> · {_html(state)}"
                    ),
                ]
            )
        return "\n".join(lines)



__all__ = ["CanonicalTelegramPublisherManager"]
