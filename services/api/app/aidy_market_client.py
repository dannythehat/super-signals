from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlencode

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

AIDY_CLIENT_ID = "super-signals-provider-lab"
AIDY_QUOTE_MODE = "aidy_m1"
_MAX_WINDOW = timedelta(hours=48)


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _utc(value: datetime | str) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class AidyM1Bar:
    open_time_utc: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


class AidyMarketClient:
    """Authenticated GET-only client for AIDY's bounded Provider Lab market feed."""

    def __init__(self, *, base_url: str, private_key_b64url: str, timeout_seconds: float = 8.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._private_key = Ed25519PrivateKey.from_private_bytes(_b64url_decode(private_key_b64url))
        self._timeout_seconds = timeout_seconds

    @classmethod
    def from_environment(cls) -> "AidyMarketClient | None":
        base_url = os.getenv("AIDY_PROVIDER_MARKET_URL", "").strip()
        private_key = os.getenv("AIDY_PROVIDER_READ_PRIVATE_KEY", "").strip()
        if not base_url or not private_key:
            return None
        return cls(base_url=base_url, private_key_b64url=private_key)

    def _signed_headers(self, *, path: str, raw_query: str) -> dict[str, str]:
        timestamp = str(int(time.time()))
        payload = f"GET\n{path}\n{raw_query}\n{timestamp}\n{AIDY_CLIENT_ID}".encode()
        return {
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "User-Agent": "SuperSignals-ProviderLab-AIDY/1.0",
            "X-AIDY-Client": AIDY_CLIENT_ID,
            "X-AIDY-Timestamp": timestamp,
            "X-AIDY-Signature": _b64url(self._private_key.sign(payload)),
        }

    async def _fetch_window(self, *, start: datetime, end: datetime) -> list[AidyM1Bar]:
        start = _utc(start)
        end = _utc(end)
        if start >= end or end - start > _MAX_WINDOW:
            raise ValueError("AIDY M1 window must be positive and at most 48 hours.")
        path = "/market/ohlc"
        raw_query = urlencode(
            [
                ("symbol", "XAUUSD"),
                ("from", start.isoformat()),
                ("to", end.isoformat()),
                ("timeframe", "1m"),
            ]
        )
        timeout = httpx.Timeout(self._timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                f"{self._base_url}{path}?{raw_query}",
                headers=self._signed_headers(path=path, raw_query=raw_query),
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise RuntimeError("AIDY market provider returned a non-success payload.")
        raw_bars = payload.get("bars")
        if not isinstance(raw_bars, list):
            raise TypeError("AIDY market provider bars payload is invalid.")
        bars: list[AidyM1Bar] = []
        for item in raw_bars:
            if not isinstance(item, dict):
                raise TypeError("AIDY M1 bar is invalid.")
            bars.append(
                AidyM1Bar(
                    open_time_utc=_utc(str(item["open_time_utc"])),
                    open=Decimal(str(item["open"])),
                    high=Decimal(str(item["high"])),
                    low=Decimal(str(item["low"])),
                    close=Decimal(str(item["close"])),
                )
            )
        return bars

    async def fetch_m1(self, *, start: datetime, end: datetime) -> list[AidyM1Bar]:
        """Fetch a long interval as bounded sequential calls; never asks AIDY for >48h."""

        cursor = _utc(start)
        end = _utc(end)
        bars: list[AidyM1Bar] = []
        while cursor < end:
            chunk_end = min(end, cursor + _MAX_WINDOW)
            bars.extend(await self._fetch_window(start=cursor, end=chunk_end))
            cursor = chunk_end
        deduped = {bar.open_time_utc: bar for bar in bars}
        return [deduped[key] for key in sorted(deduped)]
