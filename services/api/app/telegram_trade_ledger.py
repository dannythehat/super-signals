"""Telegram-only presentation for the main Super Signals reference account.

This module is read-only. It does not alter broker execution, subscriber balances,
provider risk, or AIDY authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import os
import re
from decimal import ROUND_HALF_UP, Decimal
from hashlib import sha256
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.reporting_overrides import BROKER_DEAL_NOT_OVERRIDDEN_SQL, override_cash_by_day

SOFIA = ZoneInfo("Europe/Sofia")
REFERENCE_START_AT = datetime(2026, 8, 5, 21, 0, tzinfo=UTC)
REFERENCE_START_LABEL = "6 Aug 2026"
REFERENCE_START_VALUE = Decimal("1000.00")

# A published account figure must never look current when the MetaAPI/Vantage
# capture loop has stalled. Tunable without a deploy.
ACCOUNT_SNAPSHOT_MAX_AGE_SECONDS = max(
    60, int(os.getenv("TELEGRAM_ACCOUNT_SNAPSHOT_MAX_AGE_SECONDS", "900") or 900)
)
_PROVIDER_EMOJIS = (
    "⚡", "🎯", "🏆", "👑", "💎", "🦁", "🚀", "📈", "🧭", "🔥",
    "🛡️", "🌍", "🥇", "🧠", "⭐", "🛰️", "🏹", "🔔", "🪙", "🐂",
)


def _normalise_provider_name(value: str) -> str:
    return " ".join(str(value or "").casefold().replace("’", "'").split())


def provider_badge(source_id: UUID | str | None, provider_name: str = "") -> str:
    """One recognisable provider emoji; never the old coloured-dot pair."""
    name = _normalise_provider_name(provider_name)
    if "tig's asia trades" in name or ("asia" in name and "tig" in name):
        return "🇯🇵"
    if "ftx" in name:
        return "🏎️"
    keyword_icons = (
        ("diamond", "💎"),
        ("king", "👑"),
        ("queen", "👑"),
        ("hunter", "🦁"),
        ("lion", "🦁"),
        ("sniper", "🎯"),
        ("sure", "🎯"),
        ("top 1%", "🥇"),
        ("global", "🌍"),
        ("rocket", "🚀"),
        ("scalp", "📈"),
        ("trade", "📈"),
        ("gold", "🪙"),
    )
    for keyword, icon in keyword_icons:
        if keyword in name:
            return icon
    raw = str(source_id or name or "unknown").encode("utf-8")
    return _PROVIDER_EMOJIS[sha256(raw).digest()[0] % len(_PROVIDER_EMOJIS)]


def money(value: Decimal | int | float | str | None) -> str:
    amount = Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "+" if amount > 0 else "-" if amount < 0 else ""
    return f"{sign}$" + f"{abs(amount):,.2f}"


@dataclass(frozen=True, slots=True)
class AccountLedgerSnapshot:
    account_value: Decimal | None
    mt5_balance: Decimal | None
    today_pnl: Decimal
    month_to_date_pnl: Decimal
    one_percent: Decimal | None
    local_weekday: int
    updated_at: datetime | None
    today_opening_value: Decimal | None = None
    stale: bool = False

    @property
    def weekday_trading_day(self) -> bool:
        return self.local_weekday < 5


@dataclass(frozen=True, slots=True)
class TradeLegSnapshot:
    tp_index: int
    status: str
    cash_pnl: Decimal
    target_hit: bool = False
    provider_reported_hit: bool = False


@dataclass(frozen=True, slots=True)
class TradeLedgerSnapshot:
    realised_pnl: Decimal
    total_legs: int
    closed_legs: int
    open_legs: int
    pending_legs: int
    legs: tuple[TradeLegSnapshot, ...] = ()

    @property
    def complete(self) -> bool:
        return self.total_legs > 0 and self.open_legs == 0 and self.pending_legs == 0


class TelegramTradeLedger:
    """Read-only main-account ledger for member-facing Telegram posts."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        reference_user_id: UUID,
    ) -> None:
        self._session_factory = session_factory
        self._reference_user_id = reference_user_id

    def account(self, *, now: datetime | None = None) -> AccountLedgerSnapshot:
        point = (now or datetime.now(UTC)).astimezone(UTC)
        local_point = point.astimezone(SOFIA)
        local_day = local_point.date()
        month_start = local_day.replace(day=1)

        # The owner-defined Telegram trading day is 21:00 Sofia -> 21:00 Sofia.
        # This is wall-clock local time so DST changes are handled by ZoneInfo rather
        # than a hard-coded UTC offset.
        trading_day_start_local = local_point.replace(
            hour=21, minute=0, second=0, microsecond=0
        )
        if local_point < trading_day_start_local:
            trading_day_start_local -= timedelta(days=1)
        trading_day_start = trading_day_start_local.astimezone(UTC)

        with self._session_factory() as session:
            snapshot = session.execute(
                text(
                    """
                    SELECT pas.balance,pas.equity,pas.captured_at
                    FROM performance_account_snapshots pas
                    JOIN mt5_accounts a ON a.id=pas.mt5_account_id
                    WHERE a.owner_user_id=:user_id
                      AND a.status<>'revoked'
                      AND pas.captured_at<=:point
                    ORDER BY pas.captured_at DESC
                    LIMIT 1
                    """
                ),
                {"user_id": self._reference_user_id, "point": point},
            ).mappings().first()

            trading_day_open = session.execute(
                text(
                    """
                    SELECT pas.balance,pas.equity,pas.captured_at
                    FROM performance_account_snapshots pas
                    JOIN mt5_accounts a ON a.id=pas.mt5_account_id
                    WHERE a.owner_user_id=:user_id
                      AND a.status<>'revoked'
                      AND pas.captured_at<=:trading_day_start
                    ORDER BY pas.captured_at DESC
                    LIMIT 1
                    """
                ),
                {
                    "user_id": self._reference_user_id,
                    "trading_day_start": trading_day_start,
                },
            ).mappings().first()

            rows = session.execute(
                text(
                    f"""
                    SELECT
                        timezone('Europe/Sofia',bd.occurred_at)::date AS day,
                        COALESCE(SUM(
                            COALESCE(bd.profit,0)
                            + COALESCE(bd.commission,0)
                            + COALESCE(bd.swap,0)
                        ),0) AS pnl
                    FROM broker_deals bd
                    JOIN sources src ON src.id=bd.source_id
                    WHERE bd.user_id=:user_id
                      AND src.status<>'revoked'
                      AND bd.entry_type='DEAL_ENTRY_OUT'
                      AND (bd.signal_id IS NOT NULL OR bd.broker_client_id LIKE 'SS_%')
                      AND bd.occurred_at>=:start_at
                      AND bd.occurred_at<=:end_at
                      AND {BROKER_DEAL_NOT_OVERRIDDEN_SQL}
                    GROUP BY 1
                    """
                ),
                {
                    "user_id": self._reference_user_id,
                    "start_at": REFERENCE_START_AT,
                    "end_at": point,
                },
            ).mappings().all()

            reviewed = session.execute(
                text(
                    """
                    SELECT
                        timezone('Europe/Sofia',pto.closed_at)::date AS day,
                        COALESCE(SUM(pto.cash_pnl),0) AS pnl
                    FROM performance_trade_outcomes pto
                    WHERE pto.user_id=:user_id
                      AND pto.closed_at>=:start_at
                      AND pto.closed_at<=:end_at
                      AND pto.broker_deal_count=0
                      AND pto.close_reason LIKE 'reviewed_provider_%'
                    GROUP BY 1
                    """
                ),
                {
                    "user_id": self._reference_user_id,
                    "start_at": REFERENCE_START_AT,
                    "end_at": point,
                },
            ).mappings().all()

            overrides = override_cash_by_day(
                session,
                self._reference_user_id,
                start=REFERENCE_START_AT,
                end=point,
            )
            today_opening_value = (
                Decimal(str(trading_day_open["equity"])).quantize(Decimal("0.01"))
                if trading_day_open is not None and trading_day_open["equity"] is not None
                else None
            )

        by_day: dict[object, Decimal] = {}
        for row in rows:
            day = row["day"]
            by_day[day] = by_day.get(day, Decimal("0")) + Decimal(str(row["pnl"] or 0))
        for row in reviewed:
            day = row["day"]
            by_day[day] = by_day.get(day, Decimal("0")) + Decimal(str(row["pnl"] or 0))
        for day, amount in overrides.items():
            by_day[day] = by_day.get(day, Decimal("0")) + Decimal(str(amount or 0))

        realised_today = by_day.get(local_day, Decimal("0")).quantize(Decimal("0.01"))
        month = sum(
            (
                amount
                for day, amount in by_day.items()
                if hasattr(day, "year")
                and day >= month_start
                and day <= local_day
            ),
            Decimal("0"),
        ).quantize(Decimal("0.01"))

        # Telegram's owner-facing "Balance" means the full Vantage account value:
        # broker equity (closed balance + current floating P/L). Keep raw MT5 balance
        # separately for diagnostics, but never publish it as the owner's account total.
        account_value = (
            Decimal(str(snapshot["equity"])).quantize(Decimal("0.01"))
            if snapshot is not None and snapshot["equity"] is not None
            else None
        )
        mt5_balance = (
            Decimal(str(snapshot["balance"])).quantize(Decimal("0.01"))
            if snapshot is not None and snapshot["balance"] is not None
            else None
        )
        captured_at = snapshot["captured_at"] if snapshot is not None else None
        stale = True
        if captured_at is not None:
            captured = captured_at
            if captured.tzinfo is None:
                captured = captured.replace(tzinfo=UTC)
            stale = (point - captured.astimezone(UTC)).total_seconds() > ACCOUNT_SNAPSHOT_MAX_AGE_SECONDS

        # Today's P&L is the movement in full Vantage account value from the most recent
        # 21:00 Sofia boundary. It therefore includes realised and floating P/L and resets
        # at 21:00 local time every day, exactly as the owner requested.
        today = (
            (account_value - today_opening_value).quantize(Decimal("0.01"))
            if account_value is not None and today_opening_value is not None
            else realised_today
        )

        # Month-to-date remains realised reporting; 1% sizing is always the fresh broker
        # balance and is withheld when the snapshot is stale.
        one_percent = (
            (account_value * Decimal("0.01")).quantize(Decimal("0.01"))
            if account_value is not None and not stale
            else None
        )

        return AccountLedgerSnapshot(
            account_value=account_value,
            mt5_balance=mt5_balance,
            today_pnl=today,
            month_to_date_pnl=month,
            one_percent=one_percent,
            local_weekday=local_point.weekday(),
            updated_at=captured_at,
            today_opening_value=today_opening_value,
            stale=stale,
        )

    def trade(self, signal_id: UUID) -> TradeLedgerSnapshot:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT
                        COUNT(p.id)::int AS total_legs,
                        COUNT(p.id) FILTER (
                            WHERE COALESCE(o.status,'') IN
                                ('won','lost','breakeven','closed_unknown')
                               OR (
                                  p.status='closed'
                                  AND COALESCE(o.status,'') NOT IN ('open','pending')
                               )
                        )::int AS closed_legs,
                        COUNT(p.id) FILTER (
                            WHERE p.status='open'
                              AND p.broker_position_id IS NOT NULL
                              AND COALESCE(o.status,'open') NOT IN
                                  ('won','lost','breakeven','closed_unknown')
                        )::int AS open_legs,
                        COUNT(p.id) FILTER (
                            WHERE p.status IN ('planned','pending')
                              AND COALESCE(p.broker_order_id,p.broker_position_id) IS NOT NULL
                              AND COALESCE(o.status,'pending') NOT IN
                                  ('won','lost','breakeven','closed_unknown')
                        )::int AS pending_legs,
                        COALESCE(SUM(
                            CASE
                                WHEN o.status IN ('won','lost','breakeven','closed_unknown')
                                THEN COALESCE(o.cash_pnl,0)
                                ELSE 0
                            END
                        ),0) AS realised_pnl
                    FROM positions p
                    LEFT JOIN performance_trade_outcomes o ON o.position_id=p.id
                    WHERE p.signal_id=:signal_id
                      AND p.user_id=:user_id
                    """
                ),
                {"signal_id": signal_id, "user_id": self._reference_user_id},
            ).mappings().one()
            leg_rows = session.execute(
                text(
                    """
                    SELECT
                        p.tp_index,
                        p.status AS position_status,
                        p.take_profit,
                        COALESCE(
                            p.exit_price,
                            (
                                SELECT bd.price
                                FROM broker_deals bd
                                WHERE bd.position_id=p.id
                                  AND bd.user_id=:user_id
                                  AND bd.entry_type IN ('DEAL_ENTRY_OUT','DEAL_ENTRY_OUT_BY')
                                ORDER BY bd.occurred_at DESC,bd.id DESC
                                LIMIT 1
                            )
                        ) AS effective_exit_price,
                        s.side,
                        COALESCE(o.status,'') AS outcome_status,
                        o.cash_pnl AS outcome_cash_pnl,
                        COALESCE(
                            (
                                SELECT SUM(
                                    COALESCE(bd.profit,0)
                                    + COALESCE(bd.commission,0)
                                    + COALESCE(bd.swap,0)
                                )
                                FROM broker_deals bd
                                WHERE bd.position_id=p.id
                                  AND bd.user_id=:user_id
                                  AND bd.entry_type IN ('DEAL_ENTRY_OUT','DEAL_ENTRY_OUT_BY')
                            ),
                            0
                        ) AS broker_cash_pnl,
                        (
                            SELECT COUNT(*)
                            FROM broker_deals bd
                            WHERE bd.position_id=p.id
                              AND bd.user_id=:user_id
                              AND bd.entry_type IN ('DEAL_ENTRY_OUT','DEAL_ENTRY_OUT_BY')
                        )::int AS broker_close_count
                    FROM positions p
                    JOIN signals s ON s.id=p.signal_id
                    LEFT JOIN performance_trade_outcomes o ON o.position_id=p.id
                    WHERE p.signal_id=:signal_id
                      AND p.user_id=:user_id
                    ORDER BY p.tp_index,p.created_at,p.id
                    """
                ),
                {"signal_id": signal_id, "user_id": self._reference_user_id},
            ).mappings().all()
            milestone_rows = session.execute(
                text(
                    """
                    SELECT m.raw_text
                    FROM signal_lifecycle_events e
                    JOIN messages m ON m.id=e.source_message_id
                    WHERE e.signal_id=:signal_id
                      AND e.origin='provider_update'
                    ORDER BY e.created_at
                    """
                ),
                {"signal_id": signal_id},
            ).mappings().all()

        provider_hits: set[int] = set()
        milestone_pattern = re.compile(
            r"\bTP\s*(\d+)\b.{0,48}\b(?:HIT|HITS|REACHED|TAPPED)\b"
            r"|\b(?:HIT|HITS|REACHED|TAPPED)\b.{0,24}\bTP\s*(\d+)\b",
            re.IGNORECASE | re.DOTALL,
        )
        for milestone in milestone_rows:
            for match in milestone_pattern.finditer(str(milestone["raw_text"] or "")):
                raw_index = match.group(1) or match.group(2)
                if raw_index:
                    provider_hits.add(int(raw_index))

        legs: list[TradeLegSnapshot] = []
        for leg in leg_rows:
            tp_index = int(leg["tp_index"] or 1)
            outcome = str(leg["outcome_status"] or "").lower()
            position_status = str(leg["position_status"] or "").lower()
            side = str(leg["side"] or "").upper()
            target = Decimal(str(leg["take_profit"])) if leg["take_profit"] is not None else None
            exit_price = (
                Decimal(str(leg["effective_exit_price"]))
                if leg["effective_exit_price"] is not None
                else None
            )
            outcome_cash = leg["outcome_cash_pnl"]
            broker_cash = Decimal(str(leg["broker_cash_pnl"] or 0))
            cash_pnl = (
                Decimal(str(outcome_cash))
                if outcome_cash is not None
                else broker_cash
            )
            broker_close_count = int(leg["broker_close_count"] or 0)
            target_hit = bool(
                target is not None
                and exit_price is not None
                and (
                    (side == "BUY" and exit_price >= target - Decimal("0.75"))
                    or (side == "SELL" and exit_price <= target + Decimal("0.75"))
                )
            )
            provider_reported_hit = tp_index in provider_hits

            # Broker settlement is authoritative. performance_trade_outcomes is allowed
            # to arrive later than the deal capture, so a closed broker deal is the
            # immediate truth source for cash P/L and close classification.
            if outcome == "won" and target_hit:
                state = "won"
            elif outcome == "won":
                state = "closed_profit"
            elif outcome in {"lost", "breakeven", "closed_unknown"}:
                state = outcome
            elif position_status == "closed" and broker_close_count > 0:
                if cash_pnl > 0:
                    state = "won" if target_hit else "closed_profit"
                elif cash_pnl < 0:
                    state = "lost"
                else:
                    state = "breakeven"
            elif position_status in {"cancelled", "canceled", "skipped"}:
                # Terminal broker/local truth beats any older provider milestone.
                # A cancelled/superseded order can never become pending again merely
                # because the provider previously said that TP was reached.
                state = "cancelled"
            elif position_status == "closed":
                state = "closed_unknown"
            elif provider_reported_hit:
                state = "pending"
            else:
                state = "pending"
            legs.append(
                TradeLegSnapshot(
                    tp_index=tp_index,
                    status=state,
                    cash_pnl=cash_pnl,
                    target_hit=target_hit,
                    provider_reported_hit=provider_reported_hit,
                )
            )

        realised_pnl = sum(
            (
                leg.cash_pnl
                for leg in legs
                if leg.status in {"won", "closed_profit", "lost", "breakeven", "closed_unknown"}
            ),
            Decimal("0"),
        )

        return TradeLedgerSnapshot(
            realised_pnl=realised_pnl,
            total_legs=int(row["total_legs"] or 0),
            closed_legs=int(row["closed_legs"] or 0),
            open_legs=int(row["open_legs"] or 0),
            pending_legs=int(row["pending_legs"] or 0),
            legs=tuple(legs),
        )

    @staticmethod
    def account_lines(
        snapshot: AccountLedgerSnapshot,
        *,
        include_origin: bool = False,
    ) -> list[str]:
        lines: list[str] = []
        if snapshot.weekday_trading_day:
            lines.append(f"📅 Today: {money(snapshot.today_pnl)}")
        lines.append(f"📆 Month to date: {money(snapshot.month_to_date_pnl)}")
        if snapshot.account_value is not None:
            value = "$" + f"{snapshot.account_value:,.2f}"
            # 1% is quoted from the same full Vantage account value shown above,
            # and only when the capture is fresh (see account()).
            one_percent = (
                " · 1% = $" + f"{snapshot.one_percent:,.2f}"
                if snapshot.one_percent is not None
                else ""
            )
            if snapshot.stale:
                stamp = (
                    snapshot.updated_at.astimezone(SOFIA).strftime("%H:%M")
                    if snapshot.updated_at is not None
                    else "unknown"
                )
                lines.append(
                    f"💰 Vantage balance: {value} (last updated {stamp})"
                )
            else:
                lines.append(f"💰 Vantage balance: {value}{one_percent}")
        if include_origin:
            lines.append(
                "🏁 Started $" + f"{REFERENCE_START_VALUE:,.2f}" + f" · {REFERENCE_START_LABEL}"
            )
        return lines


__all__ = [
    "AccountLedgerSnapshot",
    "REFERENCE_START_AT",
    "REFERENCE_START_LABEL",
    "REFERENCE_START_VALUE",
    "TelegramTradeLedger",
    "TradeLegSnapshot",
    "TradeLedgerSnapshot",
    "money",
    "provider_badge",
]
