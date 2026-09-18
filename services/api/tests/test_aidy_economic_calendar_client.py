"""The free economic calendar feed client: parse real shape, never raise on a bad fetch."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.aidy_economic_calendar_client import (
    EconomicCalendarClient,
    EconomicCalendarUnavailable,
)

_SAMPLE = [
    {
        "title": "Treasury Sec Bessent Speaks",
        "country": "USD",
        "date": "2026-09-15T10:00:00-04:00",
        "impact": "Medium",
        "forecast": "",
        "previous": "",
    },
    {
        "title": "PPI m/m",
        "country": "CHF",
        "date": "2026-09-14T02:30:00-04:00",
        "impact": "Low",
        "forecast": "0.0%",
        "previous": "-0.1%",
    },
]


def test_fetch_week_parses_the_real_feed_shape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("ff_calendar_thisweek.json")
        return httpx.Response(200, json=_SAMPLE, request=request)

    client = EconomicCalendarClient(transport=httpx.MockTransport(handler))

    events = asyncio.run(client.fetch_week("thisweek"))

    assert len(events) == 2
    assert events[0].title == "Treasury Sec Bessent Speaks"
    assert events[0].country == "USD"
    assert events[0].impact == "Medium"
    assert events[0].event_time_utc.tzinfo is not None
    assert events[1].forecast == "0.0%"
    assert events[1].previous == "-0.1%"


def test_a_non_list_response_is_reported_as_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not": "a list"}, request=request)

    client = EconomicCalendarClient(transport=httpx.MockTransport(handler))

    with pytest.raises(EconomicCalendarUnavailable):
        asyncio.run(client.fetch_week("thisweek"))


def test_a_transport_failure_is_reported_as_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"}, request=request)

    client = EconomicCalendarClient(transport=httpx.MockTransport(handler))

    with pytest.raises(EconomicCalendarUnavailable):
        asyncio.run(client.fetch_week("thisweek"))


def test_events_missing_a_usable_date_are_skipped_not_fatal() -> None:
    malformed = [
        {"title": "Bad Event", "country": "USD", "date": "not-a-date", "impact": "High"},
        _SAMPLE[0],
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=malformed, request=request)

    client = EconomicCalendarClient(transport=httpx.MockTransport(handler))

    events = asyncio.run(client.fetch_week("thisweek"))

    assert len(events) == 1
    assert events[0].title == "Treasury Sec Bessent Speaks"


def test_from_environment_is_enabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("AIDY_ECONOMIC_CALENDAR_ENABLED", raising=False)
    assert EconomicCalendarClient.from_environment() is not None


def test_from_environment_respects_the_kill_switch(monkeypatch) -> None:
    monkeypatch.setenv("AIDY_ECONOMIC_CALENDAR_ENABLED", "0")
    assert EconomicCalendarClient.from_environment() is None
