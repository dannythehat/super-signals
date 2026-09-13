"""Canonical weekly market freeze for Super Signals.

Standard Vantage XAU/USD closes Friday 23:57 and reopens Monday 01:01 in the
GMT+2/GMT+3 platform clock. Europe/Sofia follows that clock for the normal operating
schedule used by this app. During the weekly closure Super Signals must not monitor
provider groups, run AI interpretation, poll broker state, calculate margin, reconcile
orders, settle trades or mutate broker positions. The web/API may remain available and
serve already-stored local/cached data.
"""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

_TRADING_TIMEZONE = ZoneInfo("Europe/Sofia")
_FRIDAY_CLOSE = time(23, 57)
_MONDAY_OPEN = time(1, 1)


def market_week_frozen(now: datetime | None = None) -> bool:
    """Return True from Friday market close until the weekly XAU/USD reopen."""
    point = now or datetime.now(_TRADING_TIMEZONE)
    if point.tzinfo is None:
        point = point.replace(tzinfo=_TRADING_TIMEZONE)
    else:
        point = point.astimezone(_TRADING_TIMEZONE)

    weekday = point.weekday()  # Monday=0 ... Sunday=6
    local_time = point.timetz().replace(tzinfo=None)
    if weekday == 4:  # Friday
        return local_time >= _FRIDAY_CLOSE
    if weekday in {5, 6}:  # Saturday / Sunday
        return True
    if weekday == 0:  # Monday before weekly reopen
        return local_time < _MONDAY_OPEN
    return False


def weekend_trading_frozen(now: datetime | None = None) -> bool:
    """Backward-compatible alias for the canonical weekly market freeze."""
    return market_week_frozen(now)


__all__ = ["market_week_frozen", "weekend_trading_frozen"]
