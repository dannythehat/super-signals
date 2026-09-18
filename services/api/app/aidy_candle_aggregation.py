"""Aggregate AIDY's real 1-minute gold bars into any higher timeframe on demand.

Pure and deterministic -- no I/O, no PIT boundary of its own. The caller (aidy_reasoning_
market_tools.py) is responsible for only ever fetching bars up to a signal's own posted_at,
exactly like every other AIDY market lookup in this codebase; this module simply reshapes
whatever bars it is given.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.aidy_market_client import AidyM1Bar

# The exact set the owner asked for by name. A timeframe outside this set is refused rather
# than silently aggregated -- an ad-hoc bucket size an LLM decided to request would be an
# untested code path, not a real feature.
SUPPORTED_TIMEFRAME_MINUTES = (1, 5, 15, 30, 45, 60)


class UnsupportedTimeframe(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AggregatedCandle:
    open_time_utc: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    bar_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "open_time_utc": self.open_time_utc.isoformat(),
            "open": str(self.open),
            "high": str(self.high),
            "low": str(self.low),
            "close": str(self.close),
            "bar_count": self.bar_count,
        }


def _bucket_start(open_time_utc: datetime, *, timeframe_minutes: int) -> datetime:
    aligned_minute = (open_time_utc.minute // timeframe_minutes) * timeframe_minutes
    return open_time_utc.replace(minute=0, second=0, microsecond=0) + timedelta(
        minutes=aligned_minute
    )


def aggregate_m1_bars(
    bars: list[AidyM1Bar], *, timeframe_minutes: int
) -> list[AggregatedCandle]:
    """Group already-fetched M1 bars into `timeframe_minutes` candles.

    A bucket with fewer than a full timeframe's worth of minutes (the earliest bucket in a
    window that does not start on a clean boundary, or one with gaps in the underlying M1
    feed) is still emitted, with its true `bar_count` -- partial and reported honestly, never
    padded or dropped silently.
    """
    if timeframe_minutes not in SUPPORTED_TIMEFRAME_MINUTES:
        raise UnsupportedTimeframe(
            f"timeframe_minutes must be one of {SUPPORTED_TIMEFRAME_MINUTES}, "
            f"got {timeframe_minutes}"
        )
    if not bars:
        return []

    ordered = sorted(bars, key=lambda bar: bar.open_time_utc)
    buckets: dict[datetime, list[AidyM1Bar]] = {}
    for bar in ordered:
        start = _bucket_start(bar.open_time_utc, timeframe_minutes=timeframe_minutes)
        buckets.setdefault(start, []).append(bar)

    candles: list[AggregatedCandle] = []
    for start in sorted(buckets):
        members = buckets[start]
        candles.append(
            AggregatedCandle(
                open_time_utc=start,
                open=members[0].open,
                high=max(member.high for member in members),
                low=min(member.low for member in members),
                close=members[-1].close,
                bar_count=len(members),
            )
        )
    return candles


def lookback_window(
    *, as_of: datetime, timeframe_minutes: int, lookback_count: int
) -> tuple[datetime, datetime]:
    """The [start, end) M1 fetch window needed to produce `lookback_count` candles ending at
    (not after) `as_of`, rounded outward to whole minutes since AidyMarketClient.fetch_m1
    requires a minute-aligned window."""
    if timeframe_minutes not in SUPPORTED_TIMEFRAME_MINUTES:
        raise UnsupportedTimeframe(
            f"timeframe_minutes must be one of {SUPPORTED_TIMEFRAME_MINUTES}, "
            f"got {timeframe_minutes}"
        )
    if lookback_count <= 0:
        raise ValueError("lookback_count_must_be_positive")
    # PIT safety: `end` is `as_of` itself, floored to the minute -- never rounded forward
    # past it. The most recent bucket this produces may be partial (fewer than
    # timeframe_minutes bars); aggregate_m1_bars reports that honestly via bar_count rather
    # than padding it, which is correct -- that bucket genuinely has not finished yet as of
    # this signal's own posted time.
    end = as_of.astimezone(UTC).replace(second=0, microsecond=0)
    start = end - timedelta(minutes=timeframe_minutes * lookback_count)
    return start, end


__all__ = [
    "SUPPORTED_TIMEFRAME_MINUTES",
    "AggregatedCandle",
    "UnsupportedTimeframe",
    "aggregate_m1_bars",
    "lookback_window",
]
