"""One canonical weekend freeze for all broker-facing Super Signals activity.

The business rule is deliberately stricter than broker market hours: Saturday and
Sunday in Europe/Sofia are fully frozen. Broker-facing work resumes automatically at
00:00 Monday Sofia time. The app may still serve cached/local data and Telegram evidence
may still be captured, but no MetaAPI terminal read, margin check, order placement,
position modification, close or cancellation may leave the service while frozen.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

_TRADING_TIMEZONE = ZoneInfo("Europe/Sofia")


def weekend_trading_frozen(now: datetime | None = None) -> bool:
    """Return True throughout Saturday and Sunday in the trading timezone."""
    point = now or datetime.now(_TRADING_TIMEZONE)
    if point.tzinfo is None:
        point = point.replace(tzinfo=_TRADING_TIMEZONE)
    else:
        point = point.astimezone(_TRADING_TIMEZONE)
    return point.weekday() >= 5


__all__ = ["weekend_trading_frozen"]
