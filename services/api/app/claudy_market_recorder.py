"""Claudy Phase 0-lite point-in-time XAUUSD market recorder.

The recorder is intentionally boring: it records read-only market evidence and
operating state. It cannot create Signals, mutate Positions, publish Telegram
messages, size risk, or call a broker trade gateway.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import logging
import os
from typing import Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

from app.claudy_market_repository import ClaudyMarketRepository
from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_read_gateway import MetaApiReadGateway
from app.mt5_crypto import BrokerCredentialDecryptionError, MetaApiTokenCipher

logger = logging.getLogger(__name__)

SYMBOL = "XAUUSD"
FAST_TIMEFRAMES = ("1m", "5m")
SLOW_TIMEFRAMES = ("15m", "1h", "4h", "1d")
ALL_TIMEFRAMES = FAST_TIMEFRAMES + SLOW_TIMEFRAMES
_TIMEFRAME_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}
_SNAPSHOT_CANDLE_KEYS = {
    "1m": "latest_m1_id",
    "5m": "latest_m5_id",
    "15m": "latest_m15_id",
    "1h": "latest_h1_id",
    "4h": "latest_h4_id",
    "1d": "latest_d1_id",
}


@dataclass(frozen=True, slots=True)
class CaptureResult:
    snapshot_id: UUID | None
    status: str
    market_open: bool
    stored_candles: int
    broker_trade_action_created: bool = False


class Sleep(Protocol):
    async def __call__(self, delay: float) -> None: ...


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Claudy market recorder requires timezone-aware datetimes.")
    return value.astimezone(UTC)


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite():
        return None
    return parsed


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _session_code(now: datetime) -> str:
    """DST-aware descriptive session label; UTC remains the stored time authority."""
    now = _utc(now)
    london = now.astimezone(ZoneInfo("Europe/London"))
    new_york = now.astimezone(ZoneInfo("America/New_York"))
    tokyo = now.astimezone(ZoneInfo("Asia/Tokyo"))

    london_open = 8 <= london.hour < 17 and london.weekday() < 5
    new_york_open = 8 <= new_york.hour < 17 and new_york.weekday() < 5
    tokyo_open = 9 <= tokyo.hour < 18 and tokyo.weekday() < 5

    if london_open and new_york_open:
        return "london_new_york_overlap"
    if london_open:
        return "london"
    if new_york_open:
        return "new_york"
    if tokyo_open:
        return "asia"
    return "off_hours"


def _sanitize_positions(payloads: list[dict[str, object]]) -> list[dict[str, object]]:
    allowed = (
        "id",
        "symbol",
        "type",
        "volume",
        "openPrice",
        "currentPrice",
        "stopLoss",
        "takeProfit",
        "profit",
        "time",
        "updateTime",
        "clientId",
    )
    return [{key: row.get(key) for key in allowed if key in row} for row in payloads]


def _sanitize_orders(payloads: list[dict[str, object]]) -> list[dict[str, object]]:
    allowed = (
        "id",
        "symbol",
        "type",
        "state",
        "volume",
        "currentVolume",
        "openPrice",
        "stopLoss",
        "takeProfit",
        "time",
        "doneTime",
        "clientId",
        "positionId",
    )
    return [{key: row.get(key) for key in allowed if key in row} for row in payloads]


def _closed_candle(
    payload: dict[str, object],
    *,
    expected_timeframe: str,
    captured_at: datetime,
) -> dict[str, object] | None:
    if expected_timeframe not in _TIMEFRAME_SECONDS:
        raise ValueError("Unsupported recorder timeframe.")
    open_time = _parse_utc(payload.get("time"))
    if open_time is None:
        return None
    if open_time + timedelta(seconds=_TIMEFRAME_SECONDS[expected_timeframe]) > captured_at:
        return None

    symbol = str(payload.get("symbol") or "")
    timeframe = str(payload.get("timeframe") or "")
    if symbol != SYMBOL or timeframe != expected_timeframe:
        return None

    prices = {key: _decimal(payload.get(key)) for key in ("open", "high", "low", "close")}
    if any(value is None for value in prices.values()):
        return None
    open_price = prices["open"]
    high_price = prices["high"]
    low_price = prices["low"]
    close_price = prices["close"]
    assert open_price is not None
    assert high_price is not None
    assert low_price is not None
    assert close_price is not None
    if (
        high_price < low_price
        or high_price < open_price
        or high_price < close_price
        or low_price > open_price
        or low_price > close_price
    ):
        return None

    evidence = {
        "symbol": symbol,
        "timeframe": timeframe,
        "open_time_utc": open_time.isoformat(),
        "broker_open_time": str(payload.get("brokerTime") or "") or None,
        "open": str(prices["open"]),
        "high": str(prices["high"]),
        "low": str(prices["low"]),
        "close": str(prices["close"]),
        "tick_volume": payload.get("tickVolume"),
        "spread": payload.get("spread"),
        "volume": payload.get("volume"),
        "source": "metaapi",
    }
    return {
        **evidence,
        "open_time_utc": open_time,
        "first_observed_at": captured_at,
        "payload_digest": _digest(evidence),
    }


class ClaudyMarketRecorderService:
    def __init__(
        self,
        *,
        reference_user_id: UUID,
        repository: ClaudyMarketRepository,
        cipher: MetaApiTokenCipher,
        gateway: MetaApiReadGateway,
        market_closed_stale_seconds: float = 300.0,
    ) -> None:
        self._reference_user_id = reference_user_id
        self._repository = repository
        self._cipher = cipher
        self._gateway = gateway
        self._market_closed_stale_seconds = max(float(market_closed_stale_seconds), 60.0)

    async def capture_once(
        self,
        *,
        timeframes: tuple[str, ...] = ALL_TIMEFRAMES,
        now: datetime | None = None,
    ) -> CaptureResult:
        captured_at = _utc(now or datetime.now(UTC))
        invalid = set(timeframes) - set(ALL_TIMEFRAMES)
        if invalid:
            raise ValueError(f"Unsupported recorder timeframes: {sorted(invalid)}")

        account = self._repository.load_reference_demo_account(self._reference_user_id)
        if account is None:
            logger.warning("Claudy recorder has no non-revoked reference demo account")
            snapshot_id = self._store_snapshot(
                captured_at=captured_at,
                status="unavailable",
                availability={"reference_demo_account": "not_configured"},
                provider_state=self._repository.provider_state_summary(),
            )
            return CaptureResult(snapshot_id, "unavailable", False, 0)

        try:
            token = self._cipher.decrypt(bytes(account["metaapi_token_ciphertext"]))
        except BrokerCredentialDecryptionError:
            logger.exception("Claudy recorder could not decrypt the reference MetaAPI token")
            snapshot_id = self._store_snapshot(
                captured_at=captured_at,
                status="unavailable",
                availability={"reference_demo_account": "credential_decryption_failed"},
                provider_state=self._repository.provider_state_summary(),
            )
            return CaptureResult(snapshot_id, "unavailable", False, 0)

        account_id = str(account["metaapi_account_id"])
        availability: dict[str, object] = {
            "account_environment": "demo",
            "cross_market": "not_configured_phase0_lite",
            "external_events": "schema_ready_no_feed_adapter",
        }

        try:
            region = await self._gateway.resolve_account_region(
                token=token,
                account_id=account_id,
            )
        except MetaApiGatewayError as exc:
            availability["region"] = exc.code
            snapshot_id = self._store_snapshot(
                captured_at=captured_at,
                status="unavailable",
                availability=availability,
                provider_state=self._repository.provider_state_summary(),
            )
            return CaptureResult(snapshot_id, "unavailable", False, 0)

        async def read(name: str, awaitable: object) -> object | None:
            try:
                value = await awaitable  # type: ignore[misc]
            except MetaApiGatewayError as exc:
                availability[name] = exc.code
                return None
            availability[name] = "available"
            return value

        account_payload = await read(
            "account_information",
            self._gateway.read_account_information(
                token=token,
                account_id=account_id,
                region=region,
            ),
        )
        positions_payload = await read(
            "positions",
            self._gateway.read_positions(
                token=token,
                account_id=account_id,
                region=region,
            ),
        )
        orders_payload = await read(
            "orders",
            self._gateway.read_orders(
                token=token,
                account_id=account_id,
                region=region,
            ),
        )
        quote_payload = await read(
            "quote",
            self._gateway.read_symbol_price(
                token=token,
                account_id=account_id,
                region=region,
                symbol=SYMBOL,
            ),
        )

        stored_candles = 0
        for timeframe in timeframes:
            payloads = await read(
                f"candles_{timeframe}",
                self._gateway.read_historical_candles(
                    token=token,
                    account_id=account_id,
                    region=region,
                    symbol=SYMBOL,
                    timeframe=timeframe,
                    limit=3,
                ),
            )
            if not isinstance(payloads, list):
                continue
            for payload in payloads:
                if not isinstance(payload, dict):
                    availability[f"candles_{timeframe}"] = "invalid_payload"
                    continue
                candle = _closed_candle(
                    payload,
                    expected_timeframe=timeframe,
                    captured_at=captured_at,
                )
                if candle is None:
                    continue
                candle_id = self._repository.store_candle(candle)
                stored_candles += 1
                _ = candle_id
            # The repository identity constraint makes repeated polling idempotent.
            # Snapshot linkage is resolved from database truth after all writes.

        latest_candle_ids = self._repository.latest_candle_ids(symbol=SYMBOL)
        candle_ids: dict[str, UUID | None] = {
            column: latest_candle_ids.get(timeframe)
            for timeframe, column in _SNAPSHOT_CANDLE_KEYS.items()
        }

        quote = quote_payload if isinstance(quote_payload, dict) else {}
        bid = _decimal(quote.get("bid"))
        ask = _decimal(quote.get("ask"))
        quote_time = _parse_utc(quote.get("time"))
        quote_age = (
            max(0.0, (captured_at - quote_time).total_seconds())
            if quote_time is not None
            else None
        )
        market_open = (
            bid is not None
            and ask is not None
            and quote_age is not None
            and quote_age <= self._market_closed_stale_seconds
        )

        terminal_trade_allowed = (
            bool(account_payload.get("tradeAllowed", False))
            if isinstance(account_payload, dict)
            else None
        )
        positions = (
            _sanitize_positions(positions_payload)
            if isinstance(positions_payload, list)
            else []
        )
        orders = (
            _sanitize_orders(orders_payload)
            if isinstance(orders_payload, list)
            else []
        )
        provider_state = self._repository.provider_state_summary()

        available_components = sum(
            1 for value in availability.values() if value == "available"
        )
        expected_components = 4 + len(timeframes)
        status = (
            "complete"
            if available_components == expected_components
            else "partial"
            if available_components > 0
            else "unavailable"
        )
        if not market_open:
            availability["market_state"] = "closed_or_stale"
        else:
            availability["market_state"] = "open"

        mid = (bid + ask) / Decimal("2") if bid is not None and ask is not None else None
        spread = ask - bid if bid is not None and ask is not None else None

        snapshot = {
            "captured_at": captured_at,
            "symbol": SYMBOL,
            "capture_status": status,
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "spread": spread,
            "quote_time": quote_time,
            "quote_age_seconds": quote_age,
            "session_code": _session_code(captured_at),
            "terminal_trade_allowed": terminal_trade_allowed,
            "position_state_json": _canonical_json(positions),
            "order_state_json": _canonical_json(orders),
            "cross_market_state_json": _canonical_json(
                {"status": "not_configured_phase0_lite"}
            ),
            "provider_state_json": _canonical_json(provider_state),
            "claudy_state_json": _canonical_json(
                {
                    "phase": "0-lite",
                    "decision_engine_enabled": False,
                    "telegram_output_enabled": False,
                }
            ),
            "data_availability_json": _canonical_json(availability),
            **candle_ids,
        }
        snapshot["snapshot_digest"] = _digest(
            {
                key: str(value) if isinstance(value, (Decimal, datetime, UUID)) else value
                for key, value in snapshot.items()
                if key not in {"captured_at", "snapshot_digest"}
            }
        )
        snapshot_id = self._repository.store_snapshot(snapshot)
        return CaptureResult(snapshot_id, status, market_open, stored_candles)

    def _store_snapshot(
        self,
        *,
        captured_at: datetime,
        status: str,
        availability: dict[str, object],
        provider_state: dict[str, int],
    ) -> UUID:
        snapshot: dict[str, object] = {
            "captured_at": captured_at,
            "symbol": SYMBOL,
            "capture_status": status,
            "bid": None,
            "ask": None,
            "mid": None,
            "spread": None,
            "quote_time": None,
            "quote_age_seconds": None,
            "session_code": _session_code(captured_at),
            "terminal_trade_allowed": None,
            "position_state_json": "[]",
            "order_state_json": "[]",
            "cross_market_state_json": _canonical_json(
                {"status": "not_configured_phase0_lite"}
            ),
            "provider_state_json": _canonical_json(provider_state),
            "claudy_state_json": _canonical_json(
                {
                    "phase": "0-lite",
                    "decision_engine_enabled": False,
                    "telegram_output_enabled": False,
                }
            ),
            "data_availability_json": _canonical_json(availability),
            "latest_m1_id": None,
            "latest_m5_id": None,
            "latest_m15_id": None,
            "latest_h1_id": None,
            "latest_h4_id": None,
            "latest_d1_id": None,
        }
        snapshot["snapshot_digest"] = _digest(
            {
                key: str(value) if isinstance(value, datetime) else value
                for key, value in snapshot.items()
                if key not in {"captured_at", "snapshot_digest"}
            }
        )
        return self._repository.store_snapshot(snapshot)


class ClaudyMarketRecorderManager:
    """Failure-isolated background loop; recorder failure cannot stop the API."""

    def __init__(
        self,
        service: ClaudyMarketRecorderService,
        *,
        poll_seconds: float = 60.0,
        slow_poll_seconds: float = 300.0,
        market_closed_backoff_seconds: float = 300.0,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._service = service
        self._poll_seconds = max(float(poll_seconds), 5.0)
        self._slow_poll_seconds = max(float(slow_poll_seconds), self._poll_seconds)
        self._market_closed_backoff_seconds = max(
            float(market_closed_backoff_seconds),
            self._poll_seconds,
        )
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(),
                name="claudy-phase0-market-recorder",
            )

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        last_slow_at: datetime | None = None
        while True:
            started_at = datetime.now(UTC)
            include_slow = (
                last_slow_at is None
                or (started_at - last_slow_at).total_seconds() >= self._slow_poll_seconds
            )
            timeframes = ALL_TIMEFRAMES if include_slow else FAST_TIMEFRAMES
            if include_slow:
                last_slow_at = started_at

            try:
                result = await self._service.capture_once(
                    timeframes=timeframes,
                    now=started_at,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Claudy Phase 0-lite recorder cycle failed safely")
                delay = self._market_closed_backoff_seconds
            else:
                delay = (
                    self._poll_seconds
                    if result.market_open
                    else self._market_closed_backoff_seconds
                )
            await self._sleep(delay)


def build_claudy_market_recorder_manager(
    *,
    session_factory,
    cipher: MetaApiTokenCipher,
    gateway: MetaApiReadGateway | None = None,
) -> ClaudyMarketRecorderManager | None:
    """Build the disabled-by-default recorder from environment configuration."""
    enabled = os.getenv("SUPER_SIGNALS_CLAUDY_CAPTURE_ENABLED", "").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return None

    reference_raw = os.getenv("SUPER_SIGNALS_CLAUDY_REFERENCE_USER_ID", "").strip()
    try:
        reference_user_id = UUID(reference_raw)
    except ValueError:
        logger.error(
            "Claudy Phase 0-lite recorder disabled: reference demo user id is missing or invalid"
        )
        return None

    def positive_float(name: str, default: float) -> float | None:
        raw = os.getenv(name, str(default)).strip()
        try:
            value = float(raw)
        except ValueError:
            logger.error("Claudy Phase 0-lite recorder disabled: %s is invalid", name)
            return None
        if value <= 0:
            logger.error("Claudy Phase 0-lite recorder disabled: %s must be positive", name)
            return None
        return value

    poll = positive_float("SUPER_SIGNALS_CLAUDY_CAPTURE_POLL_SECONDS", 60.0)
    slow_poll = positive_float("SUPER_SIGNALS_CLAUDY_CAPTURE_SLOW_POLL_SECONDS", 300.0)
    closed_backoff = positive_float(
        "SUPER_SIGNALS_CLAUDY_CAPTURE_MARKET_CLOSED_BACKOFF_SECONDS",
        300.0,
    )
    stale = positive_float(
        "SUPER_SIGNALS_CLAUDY_CAPTURE_MARKET_STALE_SECONDS",
        300.0,
    )
    if None in {poll, slow_poll, closed_backoff, stale}:
        return None

    repository = ClaudyMarketRepository(session_factory)
    service = ClaudyMarketRecorderService(
        reference_user_id=reference_user_id,
        repository=repository,
        cipher=cipher,
        gateway=gateway or MetaApiReadGateway(),
        market_closed_stale_seconds=stale,
    )
    return ClaudyMarketRecorderManager(
        service,
        poll_seconds=poll,
        slow_poll_seconds=slow_poll,
        market_closed_backoff_seconds=closed_backoff,
    )
