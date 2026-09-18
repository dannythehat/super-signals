"""Pure aggregation math -- no I/O, no PIT boundary of its own (the caller owns that)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.aidy_candle_aggregation import (
    UnsupportedTimeframe,
    aggregate_m1_bars,
    lookback_window,
)
from app.aidy_market_client import AidyM1Bar


def _bar(minute: int, *, o: str, h: str, l: str, c: str) -> AidyM1Bar:  # noqa: E741
    opened = datetime(2026, 9, 18, 10, 0, tzinfo=UTC) + timedelta(minutes=minute)
    return AidyM1Bar(
        open_time_utc=opened,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(l),
        close=Decimal(c),
        revision_index=0,
        first_observed_at=opened,
        payload_digest="d" * 64,
    )


def test_empty_input_returns_no_candles() -> None:
    assert aggregate_m1_bars([], timeframe_minutes=15) == []


def test_an_unsupported_timeframe_is_refused() -> None:
    with pytest.raises(UnsupportedTimeframe):
        aggregate_m1_bars([_bar(0, o="1", h="1", l="1", c="1")], timeframe_minutes=7)


def test_one_full_timeframe_bucket_aggregates_correctly() -> None:
    # 10:00-10:14 -> one 15-minute candle starting 10:00.
    bars = [
        _bar(0, o="2400", h="2405", l="2398", c="2402"),
        _bar(7, o="2402", h="2410", l="2401", c="2408"),
        _bar(14, o="2408", h="2409", l="2390", c="2395"),
    ]

    candles = aggregate_m1_bars(bars, timeframe_minutes=15)

    assert len(candles) == 1
    candle = candles[0]
    assert candle.open_time_utc == datetime(2026, 9, 18, 10, 0, tzinfo=UTC)
    assert candle.open == Decimal("2400")  # first bar's open
    assert candle.high == Decimal("2410")  # max across all bars
    assert candle.low == Decimal("2390")  # min across all bars
    assert candle.close == Decimal("2395")  # last bar's close
    assert candle.bar_count == 3


def test_bars_split_across_two_buckets_are_kept_separate() -> None:
    bars = [
        _bar(0, o="2400", h="2401", l="2399", c="2400"),
        _bar(14, o="2400", h="2402", l="2398", c="2401"),
        _bar(15, o="2401", h="2403", l="2400", c="2402"),  # next 15-minute bucket
        _bar(29, o="2402", h="2404", l="2401", c="2403"),
    ]

    candles = aggregate_m1_bars(bars, timeframe_minutes=15)

    assert [c.open_time_utc for c in candles] == [
        datetime(2026, 9, 18, 10, 0, tzinfo=UTC),
        datetime(2026, 9, 18, 10, 15, tzinfo=UTC),
    ]
    assert [c.bar_count for c in candles] == [2, 2]


def test_a_gap_in_the_underlying_feed_is_reported_honestly_not_padded() -> None:
    bars = [
        _bar(0, o="2400", h="2401", l="2399", c="2400"),
        # minutes 1-9 missing entirely from the feed, still the same 15-minute bucket
        _bar(10, o="2401", h="2402", l="2400", c="2401"),
    ]

    candles = aggregate_m1_bars(bars, timeframe_minutes=15)

    assert len(candles) == 1
    assert candles[0].bar_count == 2  # never padded to 15


def test_unaligned_bars_still_bucket_to_the_correct_natural_boundary() -> None:
    # A bar opening at 10:07 belongs to the 10:00-10:14 (15-min) bucket, not a bucket
    # starting at 10:07.
    candles = aggregate_m1_bars(
        [_bar(7, o="1", h="1", l="1", c="1")], timeframe_minutes=15
    )
    assert candles[0].open_time_utc == datetime(2026, 9, 18, 10, 0, tzinfo=UTC)


def test_hourly_aggregation_across_an_hour_boundary() -> None:
    bars = [
        _bar(55, o="2400", h="2401", l="2399", c="2400"),  # 10:55 -> 10:00 bucket
        _bar(65, o="2400", h="2403", l="2399", c="2402"),  # 11:05 -> 11:00 bucket
    ]
    candles = aggregate_m1_bars(bars, timeframe_minutes=60)
    assert [c.open_time_utc for c in candles] == [
        datetime(2026, 9, 18, 10, 0, tzinfo=UTC),
        datetime(2026, 9, 18, 11, 0, tzinfo=UTC),
    ]


def test_lookback_window_ends_at_the_requested_as_of_never_after() -> None:
    as_of = datetime(2026, 9, 18, 10, 37, 42, tzinfo=UTC)
    start, end = lookback_window(as_of=as_of, timeframe_minutes=15, lookback_count=4)

    # Floored to the minute, never rounded forward past as_of -- rounding forward would be
    # a genuine PIT leak (fetching a bucket that has not finished yet as of this signal).
    assert end == datetime(2026, 9, 18, 10, 37, tzinfo=UTC)
    assert end <= as_of
    assert (end - start) == timedelta(minutes=15 * 4)
    assert start.second == 0 and start.microsecond == 0
    assert end.second == 0 and end.microsecond == 0


def test_lookback_window_rejects_bad_input() -> None:
    as_of = datetime(2026, 9, 18, 10, 0, tzinfo=UTC)
    with pytest.raises(UnsupportedTimeframe):
        lookback_window(as_of=as_of, timeframe_minutes=13, lookback_count=5)
    with pytest.raises(ValueError):
        lookback_window(as_of=as_of, timeframe_minutes=15, lookback_count=0)
