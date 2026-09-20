"""Conservative historical macro schedule for AIDY's reconstructed stress lab.

This module is research-only. It contains only scheduled event timestamps verified from
official source calendars/pages for the August-September 2026 stress cohort. It never
contains realized values, surprise values, forecasts, or post-release outcomes.

The records are retrospective schedule reconstruction, not exact point-in-time captures.
They may inform the research stress lab but must never be promoted to live decision
evidence or used to claim exact-PIT coverage.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

CALENDAR_CONTRACT_VERSION = "aidy_historical_official_schedule_v1"

_EVENTS: tuple[dict[str, Any], ...] = (
    {
        "title": "Job Openings and Labor Turnover Survey (JOLTS)",
        "country": "US",
        "impact": "high",
        "time_utc": "2026-08-04T14:00:00+00:00",
        "source": "BLS",
        "source_url": "https://www.bls.gov/schedule/2026/08_sched_list.htm",
    },
    {
        "title": "Employment Situation",
        "country": "US",
        "impact": "high",
        "time_utc": "2026-08-07T12:30:00+00:00",
        "source": "BLS",
        "source_url": "https://www.bls.gov/schedule/news_release/empsit.htm",
    },
    {
        "title": "Consumer Price Index",
        "country": "US",
        "impact": "high",
        "time_utc": "2026-08-12T12:30:00+00:00",
        "source": "BLS",
        "source_url": "https://www.bls.gov/schedule/2026/08_sched_list.htm",
    },
    {
        "title": "Producer Price Index",
        "country": "US",
        "impact": "high",
        "time_utc": "2026-08-13T12:30:00+00:00",
        "source": "BLS",
        "source_url": "https://www.bls.gov/schedule/2026/08_sched_list.htm",
    },
    {
        "title": "Advance Monthly Retail Sales",
        "country": "US",
        "impact": "high",
        "time_utc": "2026-08-14T12:30:00+00:00",
        "source": "US_CENSUS",
        "source_url": "https://www.census.gov/retail/release_schedule.html",
    },
    {
        "title": "FOMC Minutes",
        "country": "US",
        "impact": "high",
        "time_utc": "2026-08-19T18:00:00+00:00",
        "source": "FEDERAL_RESERVE",
        "source_url": "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260819a.htm",
    },
    {
        "title": "GDP Second Estimate Q2 2026",
        "country": "US",
        "impact": "high",
        "time_utc": "2026-08-26T12:30:00+00:00",
        "source": "BEA",
        "source_url": "https://www.bea.gov/news/schedule/full",
    },
    {
        "title": "Personal Income and Outlays",
        "country": "US",
        "impact": "high",
        "time_utc": "2026-08-26T12:30:00+00:00",
        "source": "BEA",
        "source_url": "https://www.bea.gov/news/schedule/full",
    },
    {
        "title": "Job Openings and Labor Turnover Survey (JOLTS)",
        "country": "US",
        "impact": "high",
        "time_utc": "2026-09-01T14:00:00+00:00",
        "source": "BLS",
        "source_url": "https://www.bls.gov/schedule/2026/09_sched_list.htm",
    },
    {
        "title": "ISM Manufacturing PMI",
        "country": "US",
        "impact": "high",
        "time_utc": "2026-09-01T14:00:00+00:00",
        "source": "ISM",
        "source_url": "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/pmi/july/",
    },
    {
        "title": "ISM Services PMI",
        "country": "US",
        "impact": "high",
        "time_utc": "2026-09-03T14:00:00+00:00",
        "source": "ISM",
        "source_url": "https://www.ismworld.org/supply-management-news-and-reports/reports/ism-pmi-reports/services/july/",
    },
    {
        "title": "Employment Situation",
        "country": "US",
        "impact": "high",
        "time_utc": "2026-09-04T12:30:00+00:00",
        "source": "BLS",
        "source_url": "https://www.bls.gov/schedule/news_release/empsit.htm",
    },
)


def _event_time(event: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(str(event["time_utc"])).astimezone(UTC)


def historical_schedule_for_day(as_of: datetime) -> list[dict[str, Any]]:
    target = as_of.astimezone(UTC).date()
    output: list[dict[str, Any]] = []
    for raw in _EVENTS:
        if _event_time(raw).date() != target:
            continue
        event = deepcopy(raw)
        event.update(
            {
                "forecast": "",
                "previous": "",
                "actual": None,
                "realized_outcome_available": False,
                "schedule_provenance": "retrospective_official_schedule",
                "pit_eligible": False,
                "decision_admitted": False,
                "research_only": True,
                "live_money_execution_allowed": False,
            }
        )
        output.append(event)
    output.sort(key=_event_time)
    return output


def event_timing_label(as_of: datetime, events: list[dict[str, Any]]) -> str:
    if not events:
        return "no_verified_high_impact_event_on_utc_day"
    point = as_of.astimezone(UTC)
    nearest = min(
        abs((_event_time(event) - point).total_seconds()) / 60
        for event in events
    )
    if nearest <= 30:
        return "verified_high_impact_event_within_30m"
    if nearest <= 60:
        return "verified_high_impact_event_within_60m"
    if nearest <= 120:
        return "verified_high_impact_event_within_120m"
    return "verified_high_impact_event_same_utc_day"


def attach_historical_schedule(
    market_context: dict[str, Any],
    *,
    signal_posted_at: datetime,
) -> dict[str, Any]:
    context = deepcopy(market_context)
    events = historical_schedule_for_day(signal_posted_at)
    context["calendar_contract_version"] = CALENDAR_CONTRACT_VERSION
    context["calendar_evidence_tier"] = "retrospective_official_schedule"
    context["calendar_exact_pit_claimed"] = False
    context["todays_scheduled_events"] = events
    context["event_timing"] = event_timing_label(signal_posted_at, events)
    return context


__all__ = [
    "CALENDAR_CONTRACT_VERSION",
    "attach_historical_schedule",
    "event_timing_label",
    "historical_schedule_for_day",
]
