"""Bridge between AIDY's reasoning loop and the free economic calendar feed.

Same contract as aidy_reasoning_market_tools.py: every result is JSON-safe, and a fetch
failure is reported to the model as an {"error": ...} result (or None, for the standing day
map, which is not itself an on-demand tool result), never an exception -- the reasoning pass
must still finish and produce a real annotation even when calendar data could not be fetched.

Two distinct things live here. get_economic_calendar (build_calendar_tool_executor) is an
on-demand tool the model may choose to call for a specific window/impact query. The day map
(fetch_todays_scheduled_events) is different in kind: a standing rundown of the current UTC
day's own scheduled high/medium-impact events, computed fresh and folded into every signal's
market_context automatically -- not gated behind the model deciding to ask for it. The owner
asked directly for AIDY to "map the trading day out... refer to it throughout the day"; a
tool the model might or might not call per signal does not deliver that, a standing field does.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.aidy_economic_calendar_client import (
    EconomicCalendarClient,
    EconomicCalendarEvent,
    EconomicCalendarUnavailable,
)
from app.aidy_reasoning_engine import CALENDAR_TOOL_NAME, ToolExecutor
from app.provider_fairness import session_bucket

_IMPACT_RANK = {"low": 0, "medium": 1, "high": 2}

# The feed only ever reflects the real-world current week (last/this/next); a signal old
# enough that those three weeks could not possibly cover its own posted time gets an honest
# "unavailable" rather than a silent fetch of the wrong week's events.
_MAX_SIGNAL_AGE_FOR_CALENDAR = timedelta(days=6)


async def _fetch_merged_weeks(client: EconomicCalendarClient) -> list[EconomicCalendarEvent]:
    """last/this/next week merged and de-duplicated -- the same three-week fetch both the
    on-demand tool and the standing day map need, since either's window can span a week
    boundary near Sunday/Monday midnight."""
    events: dict[tuple[str, str, datetime], EconomicCalendarEvent] = {}
    for which in ("lastweek", "thisweek", "nextweek"):
        for event in await client.fetch_week(which):  # type: ignore[arg-type]
            events[(event.title, event.country, event.event_time_utc)] = event
    return list(events.values())


def _too_old_for_current_week(as_of: datetime) -> bool:
    now = datetime.now(UTC)
    as_of_utc = as_of.astimezone(UTC)
    return abs((now - as_of_utc).total_seconds()) > _MAX_SIGNAL_AGE_FOR_CALENDAR.total_seconds()


async def fetch_todays_scheduled_events(
    client: EconomicCalendarClient, *, as_of: datetime
) -> list[dict[str, Any]] | None:
    """The standing day map: every medium/high-impact event scheduled anywhere in the UTC
    calendar day `as_of` falls in, each labelled with the same session_bucket used everywhere
    else in this codebase for Gold session segmentation. Returns None (never an empty list
    used to mean the same thing) when the feed cannot cover this signal's own day at all --
    market_context simply omits the field rather than claim a bare "no events" that might
    just be missing data.
    """
    if _too_old_for_current_week(as_of):
        return None

    as_of_utc = as_of.astimezone(UTC)
    day_start = as_of_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)

    try:
        events = await _fetch_merged_weeks(client)
    except EconomicCalendarUnavailable:
        return None

    matched = [
        event
        for event in events
        if day_start <= event.event_time_utc.astimezone(UTC) < day_end
        and event.impact.lower() in ("medium", "high")
    ]
    matched.sort(key=lambda event: event.event_time_utc)

    return [
        {
            "time_utc": event.event_time_utc.astimezone(UTC).isoformat(),
            "session": session_bucket(event.event_time_utc.astimezone(UTC)),
            "title": event.title,
            "country": event.country,
            "impact": event.impact,
            "forecast": event.forecast,
            "previous": event.previous,
        }
        for event in matched
    ]


async def fetch_calendar_summary(
    client: EconomicCalendarClient,
    *,
    as_of: datetime,
    hours_before: int,
    hours_after: int,
    min_impact: str,
) -> dict[str, Any]:
    """PIT-safe by construction and by schema: the feed exposes no realized-outcome field at
    all, so there is nothing here that could ever describe an event's actual result before
    that result was known -- only whether/when something is scheduled, its published forecast,
    and its prior reading, all legitimately public regardless of the event's own timing
    relative to `as_of`."""
    if _too_old_for_current_week(as_of):
        return {"error": "signal_too_old_for_current_calendar_window"}

    as_of_utc = as_of.astimezone(UTC)
    min_rank = _IMPACT_RANK.get(min_impact.lower(), _IMPACT_RANK["low"])
    window_start = as_of_utc - timedelta(hours=max(0, hours_before))
    window_end = as_of_utc + timedelta(hours=max(0, hours_after))

    try:
        events = await _fetch_merged_weeks(client)
    except EconomicCalendarUnavailable as exc:
        return {"error": str(exc)}

    matched = [
        event
        for event in events
        if window_start <= event.event_time_utc.astimezone(UTC) <= window_end
        and _IMPACT_RANK.get(event.impact.lower(), -1) >= min_rank
    ]
    matched.sort(key=lambda event: event.event_time_utc)

    return {
        "window_start_utc": window_start.isoformat(),
        "window_end_utc": window_end.isoformat(),
        "events": [
            {
                "title": event.title,
                "country": event.country,
                "impact": event.impact,
                "event_time_utc": event.event_time_utc.astimezone(UTC).isoformat(),
                "forecast": event.forecast,
                "previous": event.previous,
            }
            for event in matched
        ],
    }


def build_calendar_tool_executor(
    client: EconomicCalendarClient | None, *, signal_posted_at: datetime
) -> ToolExecutor | None:
    """None when no calendar client is configured -- the engine then never offers this tool."""
    if client is None:
        return None

    async def executor(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name != CALENDAR_TOOL_NAME:
            return {"error": "unknown_tool"}
        try:
            hours_before = int(arguments["hours_before"])
            hours_after = int(arguments["hours_after"])
            min_impact = str(arguments.get("min_impact") or "medium")
        except (KeyError, TypeError, ValueError):
            return {"error": "invalid_arguments"}
        hours_before = max(0, min(hours_before, 72))
        hours_after = max(0, min(hours_after, 72))
        return await fetch_calendar_summary(
            client,
            as_of=signal_posted_at,
            hours_before=hours_before,
            hours_after=hours_after,
            min_impact=min_impact,
        )

    return executor


__all__ = [
    "build_calendar_tool_executor",
    "fetch_calendar_summary",
    "fetch_todays_scheduled_events",
]
