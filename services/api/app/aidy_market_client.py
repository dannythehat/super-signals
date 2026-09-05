from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

AIDY_CLIENT_ID = "super-signals-provider-lab"
AIDY_QUOTE_MODE = "aidy_m1"
_MAX_WINDOW = timedelta(hours=48)


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + ("=" * (-len(value) % 4)))


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _utc_strict(value: datetime | str, *, field: str) -> datetime:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}_invalid_timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field}_timezone_required")
    return parsed.astimezone(UTC)


def _price(value: object, *, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field}_invalid_price") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{field}_invalid_price")
    return parsed


@dataclass(frozen=True, slots=True)
class AidyM1Bar:
    open_time_utc: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    revision_index: int
    first_observed_at: datetime
    payload_digest: str


@dataclass(frozen=True, slots=True)
class AidyM1Window:
    start: datetime
    end: datetime
    bars: tuple[AidyM1Bar, ...]
    expected_open_times: tuple[datetime, ...]
    missing_open_times: tuple[datetime, ...]

    @property
    def complete(self) -> bool:
        return not self.missing_open_times


class AidyMarketClient:
    """GET-only authenticated client for AIDY's bounded Provider Lab market feed."""

    def __init__(self, *, base_url: str, private_key_b64url: str, timeout_seconds: float = 8.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._private_key = Ed25519PrivateKey.from_private_bytes(_b64url_decode(private_key_b64url))
        self._timeout_seconds = timeout_seconds

    @classmethod
    def from_environment(cls) -> AidyMarketClient | None:
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
            "User-Agent": "SuperSignals-ProviderLab-AIDY/2.0",
            "X-AIDY-Client": AIDY_CLIENT_ID,
            "X-AIDY-Timestamp": timestamp,
            "X-AIDY-Signature": _b64url(self._private_key.sign(payload)),
        }

    @staticmethod
    def _bar(item: dict[str, object], *, start: datetime, end: datetime) -> AidyM1Bar:
        opened = _utc_strict(item.get("open_time_utc"), field="open_time_utc")
        observed = _utc_strict(item.get("first_observed_at"), field="first_observed_at")
        if opened.second or opened.microsecond:
            raise ValueError("aidy_m1_open_not_minute_aligned")
        if opened < start or opened >= end:
            raise ValueError("aidy_m1_open_outside_window")
        if observed > end:
            raise ValueError("aidy_m1_first_observed_after_pit_cutoff")
        open_price = _price(item.get("open"), field="open")
        high = _price(item.get("high"), field="high")
        low = _price(item.get("low"), field="low")
        close = _price(item.get("close"), field="close")
        if low > high or high < max(open_price, close) or low > min(open_price, close):
            raise ValueError("aidy_m1_invalid_ohlc_geometry")
        revision = int(item.get("revision_index") or 0)
        if revision < 0:
            raise ValueError("aidy_m1_revision_invalid")
        digest = str(item.get("payload_digest") or "").strip().lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("aidy_m1_payload_digest_invalid")
        return AidyM1Bar(
            open_time_utc=opened,
            open=open_price,
            high=high,
            low=low,
            close=close,
            revision_index=revision,
            first_observed_at=observed,
            payload_digest=digest,
        )

    async def fetch_m1(self, *, start: datetime, end: datetime) -> AidyM1Window:
        """Fetch exactly one bounded PIT window; the resolver owns overlap and retry semantics."""
        start = _utc_strict(start, field="start")
        end = _utc_strict(end, field="end")
        if start.second or start.microsecond or end.second or end.microsecond:
            raise ValueError("AIDY M1 request window must be minute-aligned.")
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
        async with httpx.AsyncClient(timeout=httpx.Timeout(self._timeout_seconds)) as client:
            response = await client.get(
                f"{self._base_url}{path}?{raw_query}",
                headers=self._signed_headers(path=path, raw_query=raw_query),
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise RuntimeError("AIDY market provider returned a non-success payload.")
        if str(payload.get("symbol")) != "XAUUSD" or str(payload.get("timeframe")) != "1m":
            raise ValueError("aidy_m1_response_market_mismatch")
        if _utc_strict(payload.get("from"), field="response_from") != start:
            raise ValueError("aidy_m1_response_start_mismatch")
        if _utc_strict(payload.get("to"), field="response_to") != end:
            raise ValueError("aidy_m1_response_end_mismatch")

        raw_expected = payload.get("expected_open_times")
        raw_missing = payload.get("missing_open_times")
        raw_bars = payload.get("bars")
        if not isinstance(raw_expected, list) or not isinstance(raw_missing, list) or not isinstance(raw_bars, list):
            raise TypeError("AIDY market provider continuity payload is invalid.")

        expected = tuple(_utc_strict(value, field="expected_open_time") for value in raw_expected)
        missing = tuple(_utc_strict(value, field="missing_open_time") for value in raw_missing)
        if expected != tuple(sorted(set(expected))):
            raise ValueError("aidy_m1_expected_minutes_not_strictly_unique")
        if any(value.second or value.microsecond or value < start or value >= end for value in expected):
            raise ValueError("aidy_m1_expected_minutes_invalid")
        if tuple(sorted(set(missing))) != missing or any(value not in set(expected) for value in missing):
            raise ValueError("aidy_m1_missing_minutes_invalid")

        bars: list[AidyM1Bar] = []
        seen: set[datetime] = set()
        for raw in raw_bars:
            if not isinstance(raw, dict):
                raise TypeError("AIDY M1 bar is invalid.")
            bar = self._bar(raw, start=start, end=end)
            if bar.open_time_utc in seen:
                raise ValueError("aidy_m1_duplicate_minute")
            seen.add(bar.open_time_utc)
            bars.append(bar)
        bars.sort(key=lambda item: item.open_time_utc)
        expected_set = set(expected)
        if any(bar.open_time_utc not in expected_set for bar in bars):
            raise ValueError("aidy_m1_unexpected_off_session_bar")
        calculated_missing = tuple(value for value in expected if value not in seen)
        if calculated_missing != missing:
            raise ValueError("aidy_m1_missing_contract_mismatch")
        if int(payload.get("row_count") or 0) != len(bars):
            raise ValueError("aidy_m1_row_count_mismatch")
        if int(payload.get("expected_row_count") or 0) != len(expected):
            raise ValueError("aidy_m1_expected_row_count_mismatch")
        if bool(payload.get("complete")) != (not missing):
            raise ValueError("aidy_m1_complete_flag_mismatch")
        return AidyM1Window(start, end, tuple(bars), expected, missing)
