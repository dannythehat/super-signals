from __future__ import annotations

from datetime import UTC, datetime

from app.aidy_historical_research_calendar import (
    CALENDAR_CONTRACT_VERSION,
    attach_historical_schedule,
    historical_schedule_for_day,
)


def test_calendar_contract_is_research_only_and_contains_no_realized_values() -> None:
    events = historical_schedule_for_day(
        datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    )
    assert CALENDAR_CONTRACT_VERSION == "aidy_historical_official_schedule_v1"
    assert events
    for event in events:
        assert event["research_only"] is True
        assert event["live_money_execution_allowed"] is False
        assert event["pit_eligible"] is False
        assert event["decision_admitted"] is False
        assert event["realized_outcome_available"] is False
        assert "actual" not in event
        assert event["forecast"] == ""
        assert event["previous"] == ""


def test_calendar_has_verified_high_impact_landmarks() -> None:
    aug19 = historical_schedule_for_day(
        datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    )
    assert [(item["title"], item["time_utc"]) for item in aug19] == [
        ("FOMC Minutes", "2026-08-19T18:00:00+00:00")
    ]

    sep4 = historical_schedule_for_day(
        datetime(2026, 9, 4, 8, 0, tzinfo=UTC)
    )
    assert [(item["title"], item["time_utc"]) for item in sep4] == [
        ("Employment Situation", "2026-09-04T12:30:00+00:00")
    ]


def test_calendar_attachment_never_claims_exact_pit() -> None:
    base = {
        "event_timing": "unknown",
        "research_only": True,
        "live_money_execution_allowed": False,
    }
    attached = attach_historical_schedule(
        base,
        signal_posted_at=datetime(2026, 8, 12, 12, 10, tzinfo=UTC),
    )
    assert attached["calendar_exact_pit_claimed"] is False
    assert attached["calendar_evidence_tier"] == "retrospective_official_schedule"
    assert attached["event_timing"] == "verified_high_impact_event_within_30m"
    assert attached["todays_scheduled_events"][0]["title"] == "Consumer Price Index"
