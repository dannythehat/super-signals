"""The calendar tool bridge: real data in, a JSON-safe tool result out, failures never raise."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.aidy_economic_calendar_client import EconomicCalendarEvent, EconomicCalendarUnavailable
from app.aidy_reasoning_calendar_tools import (
    build_calendar_tool_executor,
    fetch_calendar_summary,
    fetch_todays_scheduled_events,
)
from app.aidy_reasoning_engine import CALENDAR_TOOL_NAME


def _event(
    *, title: str, country: str, impact: str, hours_from_now: float
) -> EconomicCalendarEvent:
    return EconomicCalendarEvent(
        title=title,
        country=country,
        impact=impact,
        event_time_utc=datetime.now(UTC) + timedelta(hours=hours_from_now),
        forecast="1.0%",
        previous="0.9%",
    )


@dataclass
class FakeCalendarClient:
    lastweek: list[EconomicCalendarEvent] = field(default_factory=list)
    thisweek: list[EconomicCalendarEvent] = field(default_factory=list)
    nextweek: list[EconomicCalendarEvent] = field(default_factory=list)
    raise_with: Exception | None = None

    async def fetch_week(self, which: str) -> list[EconomicCalendarEvent]:
        if self.raise_with is not None:
            raise self.raise_with
        weeks = {"lastweek": self.lastweek, "thisweek": self.thisweek, "nextweek": self.nextweek}
        return weeks[which]


def test_fetch_calendar_summary_filters_by_window_and_impact() -> None:
    client = FakeCalendarClient(
        thisweek=[
            _event(title="NFP", country="USD", impact="High", hours_from_now=2),
            _event(title="Minor release", country="USD", impact="Low", hours_from_now=3),
            _event(title="Too far ahead", country="USD", impact="High", hours_from_now=100),
        ]
    )
    now = datetime.now(UTC)

    result = asyncio.run(
        fetch_calendar_summary(
            client, as_of=now, hours_before=0, hours_after=24, min_impact="medium"
        )
    )

    titles = [event["title"] for event in result["events"]]
    assert titles == ["NFP"]  # low-impact and too-far-ahead both excluded


def test_fetch_calendar_summary_refuses_a_signal_too_old_for_the_current_week() -> None:
    client = FakeCalendarClient()
    old_signal = datetime.now(UTC) - timedelta(days=30)

    result = asyncio.run(
        fetch_calendar_summary(
            client, as_of=old_signal, hours_before=24, hours_after=24, min_impact="low"
        )
    )

    assert result == {"error": "signal_too_old_for_current_calendar_window"}


def test_fetch_calendar_summary_never_exposes_a_realized_outcome_field() -> None:
    """The feed's own schema has no 'actual' field -- this test guards that the tool result
    never accidentally introduces one, since that would be the exact PIT leak this feed's
    schema was chosen to avoid."""
    client = FakeCalendarClient(
        thisweek=[_event(title="CPI", country="USD", impact="High", hours_from_now=1)]
    )

    result = asyncio.run(
        fetch_calendar_summary(
            client,
            as_of=datetime.now(UTC),
            hours_before=0,
            hours_after=6,
            min_impact="low",
        )
    )

    assert "actual" not in result["events"][0]
    assert set(result["events"][0]) == {
        "title",
        "country",
        "impact",
        "event_time_utc",
        "forecast",
        "previous",
    }


def test_a_fetch_failure_is_reported_as_an_error_not_raised() -> None:
    client = FakeCalendarClient(
        raise_with=EconomicCalendarUnavailable("economic_calendar_fetch_failed:thisweek")
    )

    result = asyncio.run(
        fetch_calendar_summary(
            client, as_of=datetime.now(UTC), hours_before=6, hours_after=6, min_impact="low"
        )
    )

    assert result == {"error": "economic_calendar_fetch_failed:thisweek"}


def test_build_calendar_tool_executor_is_none_when_no_client_configured() -> None:
    assert build_calendar_tool_executor(None, signal_posted_at=datetime.now(UTC)) is None


def test_the_built_executor_routes_arguments_through() -> None:
    client = FakeCalendarClient(
        thisweek=[_event(title="FOMC", country="USD", impact="High", hours_from_now=5)]
    )
    executor = build_calendar_tool_executor(client, signal_posted_at=datetime.now(UTC))
    assert executor is not None

    result = asyncio.run(
        executor(CALENDAR_TOOL_NAME, {"hours_before": 0, "hours_after": 24, "min_impact": "high"})
    )

    assert [event["title"] for event in result["events"]] == ["FOMC"]


def test_the_built_executor_refuses_an_unknown_tool_name() -> None:
    executor = build_calendar_tool_executor(
        FakeCalendarClient(), signal_posted_at=datetime.now(UTC)
    )
    assert executor is not None

    result = asyncio.run(executor("some_other_tool", {}))

    assert result == {"error": "unknown_tool"}


def test_the_built_executor_reports_invalid_arguments_rather_than_crashing() -> None:
    executor = build_calendar_tool_executor(
        FakeCalendarClient(), signal_posted_at=datetime.now(UTC)
    )
    assert executor is not None

    result = asyncio.run(executor(CALENDAR_TOOL_NAME, {"hours_before": "not-a-number"}))

    assert result == {"error": "invalid_arguments"}


def test_the_built_executor_clamps_out_of_range_hours() -> None:
    client = FakeCalendarClient()
    executor = build_calendar_tool_executor(client, signal_posted_at=datetime.now(UTC))
    assert executor is not None

    result = asyncio.run(
        executor(CALENDAR_TOOL_NAME, {"hours_before": 999, "hours_after": -5, "min_impact": "low"})
    )

    # Clamped to [0, 72] each side rather than rejected -- still produces a real (empty) result.
    assert "error" not in result


def _event_at(base_day: datetime, *, hour: int, impact: str, title: str) -> EconomicCalendarEvent:
    return EconomicCalendarEvent(
        title=title,
        country="USD",
        impact=impact,
        event_time_utc=base_day.replace(hour=hour, minute=0, second=0, microsecond=0),
        forecast="2.0%",
        previous="1.8%",
    )


def test_fetch_todays_scheduled_events_includes_only_todays_medium_and_high_impact() -> None:
    as_of = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    client = FakeCalendarClient(
        thisweek=[
            _event_at(as_of, hour=2, impact="High", title="CPI"),
            _event_at(as_of, hour=20, impact="Medium", title="Fed speaker"),
            _event_at(as_of, hour=10, impact="Low", title="Minor release"),
        ]
    )
    yesterday = as_of - timedelta(days=1)
    tomorrow = as_of + timedelta(days=1)
    client.thisweek.append(_event_at(yesterday, hour=23, impact="High", title="Yesterday NFP"))
    client.thisweek.append(_event_at(tomorrow, hour=1, impact="High", title="Tomorrow FOMC"))

    result = asyncio.run(fetch_todays_scheduled_events(client, as_of=as_of))

    assert result is not None
    titles = [event["title"] for event in result]
    assert titles == ["CPI", "Fed speaker"]  # ordered by time, low-impact and other days excluded


def test_fetch_todays_scheduled_events_labels_each_with_the_canonical_session_bucket() -> None:
    as_of = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    client = FakeCalendarClient(
        thisweek=[_event_at(as_of, hour=2, impact="High", title="Asia-session event")]
    )

    result = asyncio.run(fetch_todays_scheduled_events(client, as_of=as_of))

    assert result is not None
    assert result[0]["session"] == "asia"


def test_fetch_todays_scheduled_events_refuses_a_signal_too_old() -> None:
    client = FakeCalendarClient()
    old_signal = datetime.now(UTC) - timedelta(days=30)

    result = asyncio.run(fetch_todays_scheduled_events(client, as_of=old_signal))

    assert result is None


def test_fetch_todays_scheduled_events_returns_none_on_fetch_failure() -> None:
    client = FakeCalendarClient(
        raise_with=EconomicCalendarUnavailable("economic_calendar_fetch_failed:thisweek")
    )

    result = asyncio.run(fetch_todays_scheduled_events(client, as_of=datetime.now(UTC)))

    assert result is None


def test_fetch_todays_scheduled_events_never_exposes_a_realized_outcome_field() -> None:
    as_of = datetime.now(UTC)
    client = FakeCalendarClient(
        thisweek=[_event_at(as_of, hour=as_of.hour, impact="High", title="NFP")]
    )

    result = asyncio.run(fetch_todays_scheduled_events(client, as_of=as_of))

    assert result is not None
    assert set(result[0]) == {
        "time_utc",
        "session",
        "title",
        "country",
        "impact",
        "forecast",
        "previous",
    }
