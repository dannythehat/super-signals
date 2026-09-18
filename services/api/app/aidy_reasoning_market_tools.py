"""Bridge between AIDY's reasoning tool-call loop and its real, point-in-time market feed.

Every result here is JSON-safe (plain dicts/strings) because it becomes a tool output string
sent back to the model -- never a domain object. A fetch failure is reported to the model as
an {"error": ...} result, never an exception: the reasoning pass must still finish and produce
a real annotation even when candle data could not be fetched for this particular signal.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.aidy_candle_aggregation import (
    UnsupportedTimeframe,
    aggregate_m1_bars,
    lookback_window,
)
from app.aidy_market_client import AidyMarketClient
from app.aidy_reasoning_engine import CANDLE_TOOL_NAME, ToolExecutor


async def fetch_candle_summary(
    market_client: AidyMarketClient,
    *,
    as_of: datetime,
    timeframe_minutes: int,
    lookback_count: int,
) -> dict[str, Any]:
    """PIT-safe by construction: the fetch window always ends at (never after) `as_of`."""
    try:
        start, end = lookback_window(
            as_of=as_of, timeframe_minutes=timeframe_minutes, lookback_count=lookback_count
        )
    except (UnsupportedTimeframe, ValueError) as exc:
        return {"error": str(exc)}

    try:
        window = await market_client.fetch_m1(start=start, end=end)
    except Exception as exc:  # noqa: BLE001 - report to the model, never crash the pass
        return {"error": f"candle_fetch_failed:{type(exc).__name__}"}

    candles = aggregate_m1_bars(list(window.bars), timeframe_minutes=timeframe_minutes)
    return {
        "timeframe_minutes": timeframe_minutes,
        "candles": [candle.as_dict() for candle in candles[-lookback_count:]],
        "data_complete": window.complete,
    }


def build_candle_tool_executor(
    market_client: AidyMarketClient | None, *, signal_posted_at: datetime
) -> ToolExecutor | None:
    """None when no market client is configured -- the engine then never offers the tool at all."""
    if market_client is None:
        return None

    async def executor(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name != CANDLE_TOOL_NAME:
            return {"error": "unknown_tool"}
        try:
            timeframe_minutes = int(arguments["timeframe_minutes"])
            lookback_count = int(arguments["lookback_count"])
        except (KeyError, TypeError, ValueError):
            return {"error": "invalid_arguments"}
        lookback_count = max(1, min(lookback_count, 20))
        return await fetch_candle_summary(
            market_client,
            as_of=signal_posted_at,
            timeframe_minutes=timeframe_minutes,
            lookback_count=lookback_count,
        )

    return executor


__all__ = ["build_candle_tool_executor", "fetch_candle_summary"]
