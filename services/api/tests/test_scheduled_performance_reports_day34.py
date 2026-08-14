from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.scheduled_performance_reports_day34 import ScheduledReportPeriod, due_report_periods
from app.summary_notifications_day34 import Day34SummaryNotificationService


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def test_friday_21_sofia_seeds_daily_and_weekly_after_settlement_grace() -> None:
    # 2026-08-14 is Friday and Sofia is UTC+3 in August.
    before = _utc("2026-08-14T20:59:59+03:00")
    after = _utc("2026-08-14T21:00:31+03:00")

    periods = due_report_periods(before, after)

    assert [item.period_type for item in periods] == ["daily", "weekly"]
    daily, weekly = periods
    assert daily.period_start.astimezone().astimezone(UTC) == _utc(
        "2026-08-13T21:00:00+03:00"
    )
    assert daily.period_end == _utc("2026-08-14T21:00:00+03:00")
    assert weekly.period_start == _utc("2026-08-07T21:00:00+03:00")
    assert weekly.period_end == _utc("2026-08-14T21:00:00+03:00")


def test_report_does_not_fire_before_21_sofia() -> None:
    previous = _utc("2026-08-14T20:45:00+03:00")
    point = _utc("2026-08-14T20:59:59+03:00")

    assert due_report_periods(previous, point) == ()


def test_monthly_report_sent_first_at_08_for_previous_calendar_month() -> None:
    before = _utc("2026-09-01T07:59:59+03:00")
    after = _utc("2026-09-01T08:00:31+03:00")

    periods = due_report_periods(before, after)

    assert [item.period_type for item in periods] == ["monthly"]
    monthly = periods[0]
    assert monthly.period_start == _utc("2026-08-01T00:00:00+03:00")
    assert monthly.period_end == _utc("2026-09-01T00:00:00+03:00")
    assert monthly.scheduled_at == _utc("2026-09-01T08:00:00+03:00")


def test_member_report_leads_with_net_and_hides_timezone_label() -> None:
    period = ScheduledReportPeriod(
        period_type="daily",
        period_start=_utc("2026-08-13T21:00:00+03:00"),
        period_end=_utc("2026-08-14T21:00:00+03:00"),
        scheduled_at=_utc("2026-08-14T21:00:00+03:00"),
        trigger_at=_utc("2026-08-14T21:00:30+03:00"),
    )
    title, body = Day34SummaryNotificationService._render_scheduled(
        period,
        {
            "currency": "USD",
            "period_net_pnl": Decimal("1.37"),
            "realised_cash_pnl": Decimal("-27.23"),
            "floating_cash_pnl": Decimal("28.60"),
            "closed_positions": 12,
            "wins": 7,
            "losses": 3,
            "breakeven": 2,
            "open_positions": 1,
            "model_500_pnl": Decimal("-4.35"),
        },
    )

    assert title == "📊 DAILY SUPER SIGNALS P/L"
    assert "NET P/L: +$1.37" in body
    assert "Realised during period: -$27.23" in body
    assert "Floating at cutoff: +$28.60" in body
    assert "Sofia" not in body
