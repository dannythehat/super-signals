from __future__ import annotations

from datetime import UTC, datetime

from app.scheduled_performance_reports_day34 import due_report_periods


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
