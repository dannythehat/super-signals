"""Research reads history; decisions read only what was knowable at the time.

``_bar`` refuses a bar first observed after its window closed, which is the rule that
stops a decision being made on evidence AIDY could not have had. Research asks a
different question -- what did the market actually do -- over history fetched weeks
later, so for it that rule rejects every bar there is.

The first production scoring pass recorded ``research_fetch_failed:ValueError`` against
1,427 trades for exactly this reason. These tests pin both halves: research may read past
the cutoff, and the decision path still may not.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

import pytest

from app.aidy_market_client import AidyMarketClient

START = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)
END = START + timedelta(hours=1)


def bar_payload(**overrides) -> dict:
    base = {
        "open_time_utc": START.isoformat(),
        # Backfilled five weeks after the minute it describes.
        "first_observed_at": (END + timedelta(days=34)).isoformat(),
        "open": "4000",
        "high": "4010",
        "low": "3995",
        "close": "4005",
        "revision_index": 0,
        "payload_digest": "a" * 64,
    }
    base.update(overrides)
    return base


def test_a_decision_still_refuses_a_bar_observed_after_its_window() -> None:
    """The point-in-time rule, unchanged and on by default."""
    with pytest.raises(ValueError, match="first_observed_after_pit_cutoff"):
        AidyMarketClient._bar(bar_payload(), start=START, end=END)


def test_research_may_read_a_bar_observed_long_afterwards() -> None:
    bar = AidyMarketClient._bar(
        bar_payload(), start=START, end=END, enforce_pit_cutoff=False
    )

    assert bar.open_time_utc == START
    assert bar.first_observed_at > END


def test_research_relaxes_the_cutoff_and_nothing_else() -> None:
    """Every other guard is a data-integrity check and applies to both callers."""
    for field, value, expected in [
        ("open_time_utc", (START + timedelta(seconds=30)).isoformat(), "not_minute_aligned"),
        ("open_time_utc", (END + timedelta(minutes=5)).isoformat(), "outside_window"),
        ("high", "3990", "invalid_ohlc_geometry"),
        ("low", "-1", "invalid_price"),
        ("payload_digest", "short", "payload_digest_invalid"),
    ]:
        with pytest.raises(ValueError, match=expected):
            AidyMarketClient._bar(
                bar_payload(**{field: value}),
                start=START,
                end=END,
                enforce_pit_cutoff=False,
            )


def test_only_the_research_path_opts_out_of_the_cutoff() -> None:
    """A decision path that quietly disabled this would be unfalsifiable at runtime."""
    decision = inspect.getsource(AidyMarketClient.fetch_m1)
    research = inspect.getsource(AidyMarketClient.fetch_research_m1)

    assert "enforce_pit_cutoff" not in decision
    assert "enforce_pit_cutoff=False" in research
