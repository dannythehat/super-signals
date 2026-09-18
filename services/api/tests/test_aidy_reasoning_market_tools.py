"""The candle tool bridge: real data in, a JSON-safe tool result out, failures never raise."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from app.aidy_market_client import AidyM1Bar, AidyM1Window
from app.aidy_reasoning_engine import CANDLE_TOOL_NAME
from app.aidy_reasoning_market_tools import build_candle_tool_executor, fetch_candle_summary

SIGNAL_POSTED_AT = datetime(2026, 9, 18, 10, 30, tzinfo=UTC)


def _bar(minute: int) -> AidyM1Bar:
    opened = datetime(2026, 9, 18, 10, 0, tzinfo=UTC).replace(minute=minute)
    return AidyM1Bar(
        open_time_utc=opened,
        open=Decimal("2400"),
        high=Decimal("2405"),
        low=Decimal("2398"),
        close=Decimal("2402"),
        revision_index=0,
        first_observed_at=opened,
        payload_digest="d" * 64,
    )


@dataclass
class FakeMarketClient:
    bars: list[AidyM1Bar]
    complete: bool = True
    raise_with: Exception | None = None
    requested: list[tuple[datetime, datetime]] | None = None

    def __post_init__(self) -> None:
        if self.requested is None:
            self.requested = []

    async def fetch_m1(self, *, start: datetime, end: datetime) -> AidyM1Window:
        self.requested.append((start, end))
        if self.raise_with is not None:
            raise self.raise_with
        return AidyM1Window(
            start=start,
            end=end,
            bars=tuple(self.bars),
            expected_open_times=tuple(bar.open_time_utc for bar in self.bars),
            missing_open_times=() if self.complete else (start,),
        )


def test_fetch_candle_summary_aggregates_real_bars() -> None:
    client = FakeMarketClient(bars=[_bar(m) for m in (0, 15, 30)])

    result = asyncio.run(
        fetch_candle_summary(
            client, as_of=SIGNAL_POSTED_AT, timeframe_minutes=15, lookback_count=5
        )
    )

    assert result["timeframe_minutes"] == 15
    assert result["data_complete"] is True
    assert len(result["candles"]) == 3
    assert "error" not in result


def test_fetch_candle_summary_reports_an_incomplete_feed_honestly() -> None:
    client = FakeMarketClient(bars=[_bar(0)], complete=False)

    result = asyncio.run(
        fetch_candle_summary(
            client, as_of=SIGNAL_POSTED_AT, timeframe_minutes=15, lookback_count=5
        )
    )

    assert result["data_complete"] is False


def test_an_unsupported_timeframe_is_reported_as_an_error_not_raised() -> None:
    client = FakeMarketClient(bars=[])

    result = asyncio.run(
        fetch_candle_summary(
            client, as_of=SIGNAL_POSTED_AT, timeframe_minutes=13, lookback_count=5
        )
    )

    assert "error" in result
    assert client.requested == []  # never even attempted the fetch


def test_a_network_failure_is_reported_as_an_error_not_raised() -> None:
    client = FakeMarketClient(bars=[], raise_with=TimeoutError("boom"))

    result = asyncio.run(
        fetch_candle_summary(
            client, as_of=SIGNAL_POSTED_AT, timeframe_minutes=15, lookback_count=5
        )
    )

    assert result == {"error": "candle_fetch_failed:TimeoutError"}


def test_build_candle_tool_executor_is_none_when_no_market_client_configured() -> None:
    assert build_candle_tool_executor(None, signal_posted_at=SIGNAL_POSTED_AT) is None


def test_the_built_executor_routes_arguments_through_to_the_real_fetch() -> None:
    client = FakeMarketClient(bars=[_bar(0), _bar(15)])
    executor = build_candle_tool_executor(client, signal_posted_at=SIGNAL_POSTED_AT)
    assert executor is not None

    result = asyncio.run(
        executor(CANDLE_TOOL_NAME, {"timeframe_minutes": 15, "lookback_count": 5})
    )

    assert result["timeframe_minutes"] == 15
    assert client.requested[0][1] == SIGNAL_POSTED_AT  # PIT-bound to the signal's own time


def test_the_built_executor_refuses_an_unknown_tool_name() -> None:
    client = FakeMarketClient(bars=[])
    executor = build_candle_tool_executor(client, signal_posted_at=SIGNAL_POSTED_AT)
    assert executor is not None

    result = asyncio.run(executor("some_other_tool", {}))

    assert result == {"error": "unknown_tool"}


def test_the_built_executor_reports_invalid_arguments_rather_than_crashing() -> None:
    client = FakeMarketClient(bars=[])
    executor = build_candle_tool_executor(client, signal_posted_at=SIGNAL_POSTED_AT)
    assert executor is not None

    result = asyncio.run(executor(CANDLE_TOOL_NAME, {"timeframe_minutes": "not-a-number"}))

    assert result == {"error": "invalid_arguments"}


def test_the_built_executor_clamps_an_out_of_range_lookback_count() -> None:
    client = FakeMarketClient(bars=[_bar(0)])
    executor = build_candle_tool_executor(client, signal_posted_at=SIGNAL_POSTED_AT)
    assert executor is not None

    asyncio.run(executor(CANDLE_TOOL_NAME, {"timeframe_minutes": 15, "lookback_count": 999}))

    start, end = client.requested[0]
    assert (end - start).total_seconds() / 60 == 15 * 20  # clamped to the schema's max of 20
