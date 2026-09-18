"""Free, keyless public economic-calendar feed AIDY can use for real macro event awareness.

Finnhub's own economic calendar turned out to require a paid plan (verified directly against
their API: valid key, 403 "You don't have access to this resource" on /calendar/economic,
while /quote and /calendar/earnings both work free). This uses the well-known "Fair Economy"
JSON feed many retail trading tools already rely on for exactly this data -- free, no key, no
signup.

Its schema is actually safer than Finnhub's for AIDY's point-in-time discipline: it exposes
only title/country/impact/date/forecast/previous. There is no realized-outcome ("actual")
field at all, so there is nothing here that could ever leak a future-relative-to-a-signal
result -- forecast (a published consensus estimate) and previous (the prior reading) are both
legitimately public knowledge whether the event is before or after any given signal's own
posted time.

The real limitation: this feed only ever reflects the current real-world week (last/this/next
relative to whenever it is fetched), never a historical point in time. aidy_reasoning_calendar_
tools.py is responsible for refusing to use it at all when a signal is too old for "last/this/
next week" to possibly cover it, rather than risk silently serving the wrong week's events.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import httpx

WeekSelector = Literal["lastweek", "thisweek", "nextweek"]
_WEEK_FILES: dict[WeekSelector, str] = {
    "lastweek": "ff_calendar_lastweek.json",
    "thisweek": "ff_calendar_thisweek.json",
    "nextweek": "ff_calendar_nextweek.json",
}


@dataclass(frozen=True, slots=True)
class EconomicCalendarEvent:
    title: str
    country: str
    impact: str
    event_time_utc: datetime
    forecast: str
    previous: str


class EconomicCalendarUnavailable(RuntimeError):
    """The feed could not be fetched or returned something outside its known shape."""


class EconomicCalendarClient:
    def __init__(
        self,
        *,
        base_url: str = "https://nfs.faireconomy.media",
        timeout_seconds: float = 8.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        # Test-only seam: production never passes this, so real HTTP is unaffected.
        self._transport = transport

    @classmethod
    def from_environment(cls) -> EconomicCalendarClient | None:
        """No key or URL to configure -- the feed is public. The env var exists purely as an
        operational kill switch (e.g. if the free feed becomes unreliable in production)
        without needing a code change to disable it."""
        if os.getenv("AIDY_ECONOMIC_CALENDAR_ENABLED", "1").strip() == "0":
            return None
        return cls()

    async def fetch_week(self, which: WeekSelector) -> list[EconomicCalendarEvent]:
        url = f"{self._base_url}/{_WEEK_FILES[which]}"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds, transport=self._transport
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise EconomicCalendarUnavailable(f"economic_calendar_fetch_failed:{which}") from exc

        if not isinstance(payload, list):
            raise EconomicCalendarUnavailable("economic_calendar_response_not_a_list")

        events: list[EconomicCalendarEvent] = []
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            try:
                event_time = datetime.fromisoformat(str(raw["date"]))
            except (KeyError, ValueError):
                continue
            if event_time.tzinfo is None:
                continue
            events.append(
                EconomicCalendarEvent(
                    title=str(raw.get("title") or ""),
                    country=str(raw.get("country") or ""),
                    impact=str(raw.get("impact") or ""),
                    event_time_utc=event_time,
                    forecast=str(raw.get("forecast") or ""),
                    previous=str(raw.get("previous") or ""),
                )
            )
        return events


__all__ = [
    "EconomicCalendarClient",
    "EconomicCalendarEvent",
    "EconomicCalendarUnavailable",
    "WeekSelector",
]
