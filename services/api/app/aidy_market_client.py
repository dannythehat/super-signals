from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

import httpx

AIDY_QUOTE_MODE = "aidy_m1"
# Retrospective research resolution. Deliberately not AIDY_QUOTE_MODE: provider_fairness
# .score_eligibility admits only "aidy_m1" for scalper/intraday/swing styles, so a trade
# resolved from retrospective bars can never become forward evidence or clear a
# promotion gate. Its outcome is recorded separately, for the scoreboard only.
AIDY_RETROSPECTIVE_QUOTE_MODE = "aidy_m1_retrospective"
CALIBRATION_SOURCE_KIND = "calibration_backfill"
CALIBRATION_SOURCE_PROVIDER = "twelve_data"
_MAX_WINDOW = timedelta(hours=48)
_M1_RETRY_ATTEMPTS = 4
_M1_RETRY_BASE_SECONDS = 0.25
_M1_RETRY_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_CALIBRATION_RETRY_ATTEMPTS = 3
_CALIBRATION_RETRY_BASE_SECONDS = 0.2


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

    def __init__(self, *, base_url: str, bearer_token: str, timeout_seconds: float = 8.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._bearer_token = bearer_token.strip()
        if not self._bearer_token:
            raise ValueError("AIDY provider bearer token is required.")
        self._timeout_seconds = timeout_seconds

    @classmethod
    def from_environment(cls) -> AidyMarketClient | None:
        base_url = os.getenv("AIDY_PROVIDER_MARKET_URL", "").strip()
        bearer_token = os.getenv("AIDY_PROVIDER_MARKET_TOKEN", "").strip()
        if not base_url or not bearer_token:
            return None
        return cls(base_url=base_url, bearer_token=bearer_token)

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._bearer_token}",
            "Cache-Control": "no-cache",
            "User-Agent": "SuperSignals-ProviderLab-AIDY/3.0",
        }

    @staticmethod
    def _bar(
        item: dict[str, object],
        *,
        start: datetime,
        end: datetime,
        enforce_pit_cutoff: bool = True,
    ) -> AidyM1Bar:
        opened = _utc_strict(item.get("open_time_utc"), field="open_time_utc")
        observed = _utc_strict(item.get("first_observed_at"), field="first_observed_at")
        if opened.second or opened.microsecond:
            raise ValueError("aidy_m1_open_not_minute_aligned")
        if opened < start or opened >= end:
            raise ValueError("aidy_m1_open_outside_window")
        if enforce_pit_cutoff and observed > end:
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

    @staticmethod
    def _calibration_bar(item: dict[str, object], *, start: datetime, end: datetime) -> AidyM1Bar:
        """Parse retrospective calibration evidence without pretending it was PIT-known."""
        if item.get("source_kind") != CALIBRATION_SOURCE_KIND:
            raise ValueError("aidy_calibration_source_kind_invalid")
        if item.get("source_provider") != CALIBRATION_SOURCE_PROVIDER:
            raise ValueError("aidy_calibration_source_provider_invalid")
        if item.get("pit_eligible") is not False:
            raise ValueError("aidy_calibration_pit_eligible_invalid")
        if item.get("research_only") is not True:
            raise ValueError("aidy_calibration_research_only_invalid")
        if item.get("live_money_execution_allowed") is not False:
            raise ValueError("aidy_calibration_live_money_boundary_invalid")
        opened = _utc_strict(item.get("open_time_utc"), field="open_time_utc")
        observed = _utc_strict(item.get("first_observed_at"), field="first_observed_at")
        if opened.second or opened.microsecond:
            raise ValueError("aidy_calibration_m1_open_not_minute_aligned")
        if opened < start or opened >= end:
            raise ValueError("aidy_calibration_m1_open_outside_window")
        open_price = _price(item.get("open"), field="open")
        high = _price(item.get("high"), field="high")
        low = _price(item.get("low"), field="low")
        close = _price(item.get("close"), field="close")
        if low > high or high < max(open_price, close) or low > min(open_price, close):
            raise ValueError("aidy_calibration_m1_invalid_ohlc_geometry")
        revision = int(item.get("revision_index") or 0)
        if revision < 0:
            raise ValueError("aidy_calibration_m1_revision_invalid")
        digest = str(item.get("payload_digest") or "").strip().lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("aidy_calibration_m1_payload_digest_invalid")
        return AidyM1Bar(opened, open_price, high, low, close, revision, observed, digest)

    @staticmethod
    def _validate_window_payload(
        payload: dict[str, object], *, start: datetime, end: datetime, bars: list[AidyM1Bar]
    ) -> AidyM1Window:
        raw_expected = payload.get("expected_open_times")
        raw_missing = payload.get("missing_open_times")
        if not isinstance(raw_expected, list) or not isinstance(raw_missing, list):
            raise TypeError("AIDY market provider continuity payload is invalid.")
        expected = tuple(_utc_strict(value, field="expected_open_time") for value in raw_expected)
        missing = tuple(_utc_strict(value, field="missing_open_time") for value in raw_missing)
        if expected != tuple(sorted(set(expected))):
            raise ValueError("aidy_m1_expected_minutes_not_strictly_unique")
        if any(value.second or value.microsecond or value < start or value >= end for value in expected):
            raise ValueError("aidy_m1_expected_minutes_invalid")
        expected_set = set(expected)
        if tuple(sorted(set(missing))) != missing or any(value not in expected_set for value in missing):
            raise ValueError("aidy_m1_missing_minutes_invalid")
        seen: set[datetime] = set()
        for bar in bars:
            if bar.open_time_utc in seen:
                raise ValueError("aidy_m1_duplicate_minute")
            seen.add(bar.open_time_utc)
        bars.sort(key=lambda item: item.open_time_utc)
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

    async def _get_with_retry(self, *, url: str) -> httpx.Response:
        """Retry only transient transport/server failures; never relax AIDY data validation."""
        timeout = httpx.Timeout(self._timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            for attempt in range(_M1_RETRY_ATTEMPTS):
                try:
                    response = await client.get(url, headers=self._headers())
                except (httpx.ConnectTimeout, httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError):
                    if attempt + 1 >= _M1_RETRY_ATTEMPTS:
                        raise
                    await asyncio.sleep(_M1_RETRY_BASE_SECONDS * (2 ** attempt))
                    continue
                if response.status_code in _M1_RETRY_STATUS_CODES and attempt + 1 < _M1_RETRY_ATTEMPTS:
                    await asyncio.sleep(_M1_RETRY_BASE_SECONDS * (2 ** attempt))
                    continue
                response.raise_for_status()
                return response
        raise RuntimeError("AIDY M1 retry bound exhausted.")

    async def fetch_m1(self, *, start: datetime, end: datetime) -> AidyM1Window:
        """Fetch one bounded PIT window with bounded transport resilience."""
        start = _utc_strict(start, field="start")
        end = _utc_strict(end, field="end")
        if start.second or start.microsecond or end.second or end.microsecond:
            raise ValueError("AIDY M1 request window must be minute-aligned.")
        if start >= end or end - start > _MAX_WINDOW:
            raise ValueError("AIDY M1 window must be positive and at most 48 hours.")
        raw_query = urlencode([("symbol", "XAUUSD"), ("from", start.isoformat()), ("to", end.isoformat()), ("timeframe", "1m")])
        response = await self._get_with_retry(url=f"{self._base_url}/market/ohlc?{raw_query}")
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise RuntimeError("AIDY market provider returned a non-success payload.")
        if str(payload.get("symbol")) != "XAUUSD" or str(payload.get("timeframe")) != "1m":
            raise ValueError("aidy_m1_response_market_mismatch")
        if _utc_strict(payload.get("from"), field="response_from") != start:
            raise ValueError("aidy_m1_response_start_mismatch")
        if _utc_strict(payload.get("to"), field="response_to") != end:
            raise ValueError("aidy_m1_response_end_mismatch")
        raw_bars = payload.get("bars")
        if not isinstance(raw_bars, list):
            raise TypeError("AIDY market provider continuity payload is invalid.")
        bars: list[AidyM1Bar] = []
        for raw in raw_bars:
            if not isinstance(raw, dict):
                raise TypeError("AIDY M1 bar is invalid.")
            bars.append(self._bar(raw, start=start, end=end))
        return self._validate_window_payload(payload, start=start, end=end, bars=bars)

    async def fetch_research_m1(self, *, start: datetime, end: datetime) -> AidyM1Window:
        """Fetch settled history for provider research, never for a decision."""
        start = _utc_strict(start, field="start")
        end = _utc_strict(end, field="end")
        if start.second or start.microsecond or end.second or end.microsecond:
            raise ValueError("AIDY research M1 request window must be minute-aligned.")
        if start >= end or end - start > _MAX_WINDOW:
            raise ValueError("AIDY research M1 window must be positive and at most 48 hours.")
        raw_query = urlencode([("symbol", "XAUUSD"), ("from", start.isoformat()), ("to", end.isoformat()), ("timeframe", "1m")])
        async with httpx.AsyncClient(timeout=httpx.Timeout(self._timeout_seconds)) as client:
            response = await client.get(f"{self._base_url}/research/market/ohlc?{raw_query}", headers=self._headers())
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise RuntimeError("AIDY research provider returned a non-success payload.")
        claims_pit = payload.get("pit_eligible") is not False
        claims_admission = payload.get("decision_admitted") is not False
        if claims_pit or claims_admission:
            raise RuntimeError("aidy_research_response_claims_decision_admission")
        if str(payload.get("symbol")) != "XAUUSD" or str(payload.get("timeframe")) != "1m":
            raise ValueError("aidy_research_response_market_mismatch")
        if _utc_strict(payload.get("from"), field="response_from") != start:
            raise ValueError("aidy_research_response_start_mismatch")
        if _utc_strict(payload.get("to"), field="response_to") != end:
            raise ValueError("aidy_research_response_end_mismatch")
        raw_bars = payload.get("bars")
        if not isinstance(raw_bars, list):
            raise TypeError("AIDY research continuity payload is invalid.")
        bars = [self._bar(raw, start=start, end=end, enforce_pit_cutoff=False) for raw in raw_bars if isinstance(raw, dict)]
        expected = tuple(_utc_strict(value, field="expected_open_time") for value in (payload.get("expected_open_times") or []))
        missing = tuple(_utc_strict(value, field="missing_open_time") for value in (payload.get("missing_open_times") or []))
        return AidyM1Window(start, end, tuple(bars), expected, missing)

    async def fetch_calibration_m1(self, *, window_id: str, start: datetime, end: datetime) -> AidyM1Window:
        """Fetch a frozen retrospective Twelve window for Day 11 calibration only."""
        start = _utc_strict(start, field="start")
        end = _utc_strict(end, field="end")
        normalized_window_id = str(window_id).strip()
        if not normalized_window_id:
            raise ValueError("aidy_calibration_window_id_required")
        if start.second or start.microsecond or end.second or end.microsecond:
            raise ValueError("AIDY calibration M1 window must be minute-aligned.")
        if start >= end or end - start > _MAX_WINDOW:
            raise ValueError("AIDY calibration M1 window must be positive and at most 48 hours.")
        raw_query = urlencode([("window_id", normalized_window_id), ("symbol", "XAUUSD"), ("from", start.isoformat()), ("to", end.isoformat()), ("timeframe", "1m")])
        async with httpx.AsyncClient(timeout=httpx.Timeout(self._timeout_seconds)) as client:
            for attempt in range(_CALIBRATION_RETRY_ATTEMPTS):
                response = await client.get(f"{self._base_url}/calibration/market/ohlc?{raw_query}", headers=self._headers())
                if response.status_code in {500, 503} and attempt + 1 < _CALIBRATION_RETRY_ATTEMPTS:
                    await asyncio.sleep(_CALIBRATION_RETRY_BASE_SECONDS * (2 ** attempt))
                    continue
                response.raise_for_status()
                payload = response.json()
                break
            else:
                raise RuntimeError("AIDY calibration retry bound exhausted.")
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise RuntimeError("AIDY calibration market provider returned a non-success payload.")
        if str(payload.get("window_id")) != normalized_window_id:
            raise ValueError("aidy_calibration_window_id_mismatch")
        if str(payload.get("symbol")) != "XAUUSD" or str(payload.get("timeframe")) != "1m":
            raise ValueError("aidy_calibration_m1_response_market_mismatch")
        if _utc_strict(payload.get("from"), field="response_from") != start:
            raise ValueError("aidy_calibration_m1_response_start_mismatch")
        if _utc_strict(payload.get("to"), field="response_to") != end:
            raise ValueError("aidy_calibration_m1_response_end_mismatch")
        if payload.get("source_kind") != CALIBRATION_SOURCE_KIND:
            raise ValueError("aidy_calibration_response_source_kind_invalid")
        if payload.get("source_provider") != CALIBRATION_SOURCE_PROVIDER:
            raise ValueError("aidy_calibration_response_source_provider_invalid")
        if payload.get("pit_eligible") is not False:
            raise ValueError("aidy_calibration_response_pit_eligible_invalid")
        if payload.get("research_only") is not True:
            raise ValueError("aidy_calibration_response_research_only_invalid")
        if payload.get("live_money_execution_allowed") is not False:
            raise ValueError("aidy_calibration_response_live_money_boundary_invalid")
        raw_bars = payload.get("bars")
        if not isinstance(raw_bars, list):
            raise TypeError("AIDY calibration continuity payload is invalid.")
        bars: list[AidyM1Bar] = []
        for raw in raw_bars:
            if not isinstance(raw, dict):
                raise TypeError("AIDY calibration M1 bar is invalid.")
            bars.append(self._calibration_bar(raw, start=start, end=end))
        return self._validate_window_payload(payload, start=start, end=end, bars=bars)
