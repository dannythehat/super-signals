"""Scheduled Telegram performance reports on the Owner-approved Sofia clock.

Daily reports run Monday-Friday and cover 21:00 -> 21:00 Europe/Sofia. Weekly reports close every
Friday at 21:00 and cover the preceding seven days. Monthly reports are sent on
the first day of the month at 08:00 and cover the complete previous calendar
month. A short settlement grace keeps trades closing exactly on the boundary
from being omitted.

Reports use broker-derived performance outcomes. Realised paper P/L is reported
separately from current floating P/L; an open position is never counted as
realised profit or loss. Account balances and account identifiers remain private.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

SOFIA = ZoneInfo("Europe/Sofia")
_SETTLEMENT_GRACE = timedelta(seconds=30)


@dataclass(frozen=True, slots=True)
class ScheduledReportPeriod:
    period_type: str
    period_start: datetime
    period_end: datetime
    scheduled_at: datetime
    trigger_at: datetime


@dataclass(frozen=True, slots=True)
class SummarySeedResult:
    created: int
    checked_at: datetime
    broker_trade_action_created: bool = False


def _local(day: date, hour: int) -> datetime:
    return datetime.combine(day, time(hour=hour), tzinfo=SOFIA)


def _previous_month_start(month_start: datetime) -> datetime:
    previous_day = month_start.date() - timedelta(days=1)
    return datetime(previous_day.year, previous_day.month, 1, tzinfo=SOFIA)


def due_report_periods(
    last_checked_at: datetime,
    now: datetime,
) -> tuple[ScheduledReportPeriod, ...]:
    """Return scheduled reports whose grace-trigger crossed since the previous check."""

    previous = last_checked_at.astimezone(UTC)
    point = now.astimezone(UTC)
    if point <= previous:
        return ()

    previous_local = previous.astimezone(SOFIA)
    point_local = point.astimezone(SOFIA)
    cursor = previous_local.date() - timedelta(days=1)
    final_day = point_local.date()
    periods: list[ScheduledReportPeriod] = []

    while cursor <= final_day:
        daily_end_local = _local(cursor, 21)
        daily_trigger = (daily_end_local + _SETTLEMENT_GRACE).astimezone(UTC)
        if cursor.weekday() < 5 and previous < daily_trigger <= point:
            daily_start_local = _local(cursor - timedelta(days=1), 21)
            periods.append(
                ScheduledReportPeriod(
                    period_type="daily",
                    period_start=daily_start_local.astimezone(UTC),
                    period_end=daily_end_local.astimezone(UTC),
                    scheduled_at=daily_end_local.astimezone(UTC),
                    trigger_at=daily_trigger,
                )
            )

        if cursor.weekday() == 4:  # Friday
            weekly_end_local = daily_end_local
            weekly_trigger = (weekly_end_local + _SETTLEMENT_GRACE).astimezone(UTC)
            if previous < weekly_trigger <= point:
                weekly_start_local = _local(cursor - timedelta(days=7), 21)
                periods.append(
                    ScheduledReportPeriod(
                        period_type="weekly",
                        period_start=weekly_start_local.astimezone(UTC),
                        period_end=weekly_end_local.astimezone(UTC),
                        scheduled_at=weekly_end_local.astimezone(UTC),
                        trigger_at=weekly_trigger,
                    )
                )

        if cursor.day == 1:
            month_start_local = datetime(cursor.year, cursor.month, 1, tzinfo=SOFIA)
            monthly_scheduled_local = _local(cursor, 8)
            monthly_trigger = (monthly_scheduled_local + _SETTLEMENT_GRACE).astimezone(UTC)
            if previous < monthly_trigger <= point:
                periods.append(
                    ScheduledReportPeriod(
                        period_type="monthly",
                        period_start=_previous_month_start(month_start_local).astimezone(UTC),
                        period_end=month_start_local.astimezone(UTC),
                        scheduled_at=monthly_scheduled_local.astimezone(UTC),
                        trigger_at=monthly_trigger,
                    )
                )

        cursor += timedelta(days=1)

    order = {"daily": 0, "weekly": 1, "monthly": 2}
    periods.sort(key=lambda item: (item.trigger_at, order[item.period_type]))
    return tuple(periods)


class Day34ScheduledPerformanceReportService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        reference_user_id: UUID,
    ) -> None:
        self._session_factory = session_factory
        self._reference_user_id = reference_user_id

    def seed_due(self, *, now: datetime | None = None) -> SummarySeedResult:
        point = (now or datetime.now(UTC)).astimezone(UTC)
        with self._session_factory() as session:
            state = session.execute(
                text(
                    """
                    SELECT publish_after, last_checked_at, last_seeded_period_end
                    FROM day34_summary_state
                    WHERE id=1
                    """
                )
            ).mappings().one()
            publish_after = state["publish_after"].astimezone(UTC)
            last_checked = state["last_checked_at"]
            watermark = (
                last_checked.astimezone(UTC)
                if last_checked is not None
                else publish_after
            )
            if watermark < publish_after:
                watermark = publish_after

            created = 0
            last_period_end = state["last_seeded_period_end"]
            for period in due_report_periods(watermark, point):
                metrics = self._metrics(
                    session,
                    start=period.period_start,
                    end=period.period_end,
                    report_time=period.scheduled_at,
                )
                title, body = self._render(period, metrics)
                event_key = self._event_key(period)
                inserted = session.execute(
                    text(
                        """
                        INSERT INTO notification_events (
                            event_key, signal_id, lifecycle_event_id, user_id,
                            audience, kind, title, body, payload
                        ) VALUES (
                            :event_key, NULL, NULL, NULL,
                            'shared', :kind, :title, :body,
                            CAST(:payload AS jsonb)
                        )
                        ON CONFLICT (event_key) DO NOTHING
                        RETURNING id
                        """
                    ),
                    {
                        "event_key": event_key,
                        "kind": f"summary_{period.period_type}",
                        "title": title,
                        "body": body,
                        "payload": json.dumps(
                            {
                                "scheduled_performance_report": True,
                                "period_type": period.period_type,
                                "timezone": "Europe/Sofia",
                                "period_start": period.period_start.isoformat(),
                                "period_end": period.period_end.isoformat(),
                                "scheduled_at": period.scheduled_at.isoformat(),
                                "closed_positions": metrics["closed_positions"],
                                "wins": metrics["wins"],
                                "losses": metrics["losses"],
                                "breakeven": metrics["breakeven"],
                                "open_positions": metrics["open_positions"],
                                "realised_cash_pnl": self._plain(metrics["realised_cash_pnl"]),
                                "floating_cash_pnl": self._plain(metrics["floating_cash_pnl"]),
                                "currency": metrics["currency"],
                                "model_500_pnl": self._plain(metrics["model_500_pnl"]),
                                "model_500_return_percent": self._plain(
                                    metrics["model_500_return_percent"]
                                ),
                                "real_user_balance_exposed": False,
                                "reference_paper_pnl_exposed": True,
                                "provider_identity_exposed": False,
                                "broker_trade_action_created": False,
                            }
                        ),
                    },
                ).scalar_one_or_none()
                if inserted is not None:
                    created += 1
                if last_period_end is None or period.period_end > last_period_end:
                    last_period_end = period.period_end

            session.execute(
                text(
                    """
                    UPDATE day34_summary_state
                    SET last_seeded_period_end=COALESCE(:last_period_end,last_seeded_period_end),
                        last_checked_at=:now,
                        updated_at=:now
                    WHERE id=1
                    """
                ),
                {"last_period_end": last_period_end, "now": point},
            )
            session.commit()

        return SummarySeedResult(created=created, checked_at=point)

    def _metrics(
        self,
        session: Session,
        *,
        start: datetime,
        end: datetime,
        report_time: datetime,
    ) -> dict[str, Any]:
        closed = session.execute(
            text(
                """
                SELECT
                    COUNT(*) FILTER (
                        WHERE closed_at>=:start AND closed_at<:end
                          AND status IN ('won','lost','breakeven','closed_unknown')
                    )::int AS closed_positions,
                    COUNT(*) FILTER (
                        WHERE closed_at>=:start AND closed_at<:end AND status='won'
                    )::int AS wins,
                    COUNT(*) FILTER (
                        WHERE closed_at>=:start AND closed_at<:end AND status='lost'
                    )::int AS losses,
                    COUNT(*) FILTER (
                        WHERE closed_at>=:start AND closed_at<:end AND status='breakeven'
                    )::int AS breakeven,
                    COALESCE(SUM(cash_pnl) FILTER (
                        WHERE closed_at>=:start AND closed_at<:end AND cash_pnl IS NOT NULL
                    ),0) AS realised_cash_pnl,
                    COALESCE(SUM(model_500_pnl) FILTER (
                        WHERE closed_at>=:start AND closed_at<:end AND model_500_pnl IS NOT NULL
                    ),0) AS model_500_pnl,
                    COALESCE(SUM(model_500_return_percent) FILTER (
                        WHERE closed_at>=:start AND closed_at<:end
                          AND model_500_return_percent IS NOT NULL
                    ),0) AS model_500_return_percent
                FROM performance_trade_outcomes
                WHERE user_id=:user_id
                """
            ),
            {"user_id": self._reference_user_id, "start": start, "end": end},
        ).mappings().one()

        open_positions = int(
            session.execute(
                text(
                    """
                    SELECT COUNT(*)::int
                    FROM performance_trade_outcomes
                    WHERE user_id=:user_id AND status='open'
                    """
                ),
                {"user_id": self._reference_user_id},
            ).scalar_one()
        )

        snapshot = session.execute(
            text(
                """
                SELECT currency, balance, equity
                FROM performance_account_snapshots
                WHERE user_id=:user_id AND captured_at<=:report_time
                ORDER BY captured_at DESC
                LIMIT 1
                """
            ),
            {"user_id": self._reference_user_id, "report_time": report_time},
        ).mappings().first()
        currency = str(snapshot["currency"] or "USD") if snapshot else "USD"
        floating = (
            Decimal(str(snapshot["equity"])) - Decimal(str(snapshot["balance"]))
            if snapshot is not None
            else None
        )

        return {
            "closed_positions": int(closed["closed_positions"]),
            "wins": int(closed["wins"]),
            "losses": int(closed["losses"]),
            "breakeven": int(closed["breakeven"]),
            "realised_cash_pnl": Decimal(str(closed["realised_cash_pnl"] or 0)),
            "model_500_pnl": Decimal(str(closed["model_500_pnl"] or 0)),
            "model_500_return_percent": Decimal(
                str(closed["model_500_return_percent"] or 0)
            ),
            "open_positions": open_positions,
            "floating_cash_pnl": floating,
            "currency": currency,
        }

    @staticmethod
    def _event_key(period: ScheduledReportPeriod) -> str:
        return (
            f"summary-v2:{period.period_type}:"
            f"{period.period_start.astimezone(UTC).isoformat()}"
        )

    @staticmethod
    def _render(period: ScheduledReportPeriod, metrics: dict[str, Any]) -> tuple[str, str]:
        label = period.period_type.upper()
        local_start = period.period_start.astimezone(SOFIA)
        local_end = period.period_end.astimezone(SOFIA)
        currency = str(metrics["currency"] or "USD")

        if period.period_type == "daily":
            period_line = (
                f"Period: {local_start:%d %b %H:%M} → {local_end:%d %b %H:%M} Sofia"
            )
        elif period.period_type == "weekly":
            period_line = (
                f"Period: {local_start:%a %d %b %H:%M} → "
                f"{local_end:%a %d %b %H:%M} Sofia"
            )
        else:
            period_line = (
                f"Period: {local_start:%d %b %Y} → {local_end:%d %b %Y} Sofia"
            )

        lines = [
            period_line,
            "Realised paper P/L: "
            + Day34ScheduledPerformanceReportService._money(
                metrics["realised_cash_pnl"], currency
            ),
            (
                f"Closed positions: {metrics['closed_positions']} · "
                f"Won: {metrics['wins']} · Lost: {metrics['losses']} · "
                f"BE: {metrics['breakeven']}"
            ),
        ]
        floating = metrics["floating_cash_pnl"]
        floating_text = (
            Day34ScheduledPerformanceReportService._money(floating, currency)
            if floating is not None
            else "unavailable"
        )
        lines.append(
            f"Open at report time: {metrics['open_positions']} · Floating P/L: {floating_text}"
        )
        lines.append(
            "$500 model @ 1%: "
            + Day34ScheduledPerformanceReportService._money(metrics["model_500_pnl"], "USD")
        )
        lines.append("Broker-derived paper results · open P/L is not realised")
        return f"📊 {label} SUPER SIGNALS P/L", "\n".join(lines)

    @staticmethod
    def _money(value: Any, currency: str) -> str:
        amount = Decimal(str(value or 0))
        sign = "+" if amount > 0 else "-" if amount < 0 else ""
        symbol = "$" if currency.upper() == "USD" else f"{currency.upper()} "
        return f"{sign}{symbol}{abs(amount):.2f}"

    @staticmethod
    def _plain(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, Decimal):
            return format(value.normalize(), "f")
        return str(value)


__all__ = [
    "Day34ScheduledPerformanceReportService",
    "ScheduledReportPeriod",
    "SummarySeedResult",
    "due_report_periods",
]
