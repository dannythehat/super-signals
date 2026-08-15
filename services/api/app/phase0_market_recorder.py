"""Phase 0-lite point-in-time market recorder.

This module is deliberately read-only at the broker boundary. It records what was
known at the time without influencing signal acceptance, risk, routing or execution.
Recorder failures are evidence/logging events only and must never block trading.
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
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_read_gateway import MetaApiReadGateway
from app.mt5_crypto import BrokerCredentialDecryptionError, MetaApiTokenCipher

logger = logging.getLogger(__name__)

PHASE0_SOURCE = "metaapi_vantage"
PHASE0_SYMBOL = "XAUUSD"
FAST_TIMEFRAMES = ("1m", "5m")
SLOW_TIMEFRAMES = ("15m", "1h", "4h", "1d")
_TIMEFRAME_SECONDS = {
    "1m": 60,
    "5m": 5 * 60,
    "15m": 15 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
    "1d": 24 * 60 * 60,
}


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        return _utc(datetime.fromisoformat(raw))
    except ValueError:
        return None


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        return _utc(value).isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    raise TypeError(f"Unsupported canonical JSON value: {type(value)!r}")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    )


def _digest(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Phase0RecorderConfig:
    enabled: bool
    reference_user_id: UUID | None
    symbol: str = PHASE0_SYMBOL
    source: str = PHASE0_SOURCE
    fast_poll_seconds: int = 60
    slow_poll_seconds: int = 300

    @classmethod
    def from_env(cls) -> "Phase0RecorderConfig":
        enabled = _parse_bool(os.getenv("SUPER_SIGNALS_PHASE0_RECORDER_ENABLED", "0"))
        raw_user_id = os.getenv("SUPER_SIGNALS_PHASE0_REFERENCE_USER_ID", "").strip()
        reference_user_id: UUID | None = None
        if raw_user_id:
            try:
                reference_user_id = UUID(raw_user_id)
            except ValueError as exc:
                raise ValueError("SUPER_SIGNALS_PHASE0_REFERENCE_USER_ID must be a UUID") from exc
        symbol = os.getenv("SUPER_SIGNALS_PHASE0_SYMBOL", PHASE0_SYMBOL).strip().upper()
        if symbol != PHASE0_SYMBOL:
            raise ValueError("Phase 0-lite is locked to XAUUSD")
        return cls(
            enabled=enabled,
            reference_user_id=reference_user_id,
            symbol=symbol,
            fast_poll_seconds=_positive_int("SUPER_SIGNALS_PHASE0_FAST_POLL_SECONDS", 60),
            slow_poll_seconds=_positive_int("SUPER_SIGNALS_PHASE0_SLOW_POLL_SECONDS", 300),
        )


@dataclass(frozen=True, slots=True)
class Phase0AccountAccess:
    local_account_id: UUID
    metaapi_account_id: str
    token: str
    region: str


class Phase0MarketRecorder:
    """Persist immutable point-in-time market evidence without trading authority."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        cipher: MetaApiTokenCipher,
        gateway: MetaApiReadGateway,
        config: Phase0RecorderConfig,
    ) -> None:
        self._session_factory = session_factory
        self._cipher = cipher
        self._gateway = gateway
        self._config = config

    async def capture_cycle(self, *, include_slow: bool, now: datetime | None = None) -> None:
        observed_at = _utc(now or datetime.now(UTC))
        access = await self._resolve_access(observed_at)
        if access is None:
            return

        await self._capture_snapshot(access, observed_at)
        timeframes = FAST_TIMEFRAMES + (SLOW_TIMEFRAMES if include_slow else ())
        for timeframe in timeframes:
            try:
                await self._capture_closed_candles(access, timeframe, observed_at)
            except Exception:
                # Recorder faults are deliberately isolated from execution. A single
                # timeframe failure must not suppress the remaining evidence streams.
                logger.exception("Phase 0 candle capture failed timeframe=%s", timeframe)

    async def _resolve_access(self, observed_at: datetime) -> Phase0AccountAccess | None:
        reference_user_id = self._config.reference_user_id
        if reference_user_id is None:
            self._store_failed_snapshot(
                observed_at=observed_at,
                local_account_id=None,
                code="phase0_reference_user_missing",
                stage="configuration",
            )
            return None

        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT id, metaapi_account_id, metaapi_token_ciphertext
                    FROM mt5_accounts
                    WHERE owner_user_id=:user_id
                      AND status != 'revoked'
                    LIMIT 1
                    """
                ),
                {"user_id": reference_user_id},
            ).mappings().first()

        if row is None:
            self._store_failed_snapshot(
                observed_at=observed_at,
                local_account_id=None,
                code="mt5_account_not_configured",
                stage="account_lookup",
            )
            return None

        local_account_id = row["id"]
        try:
            token = self._cipher.decrypt(bytes(row["metaapi_token_ciphertext"]))
        except BrokerCredentialDecryptionError:
            self._store_failed_snapshot(
                observed_at=observed_at,
                local_account_id=local_account_id,
                code="broker_credential_decryption_failed",
                stage="decrypt",
            )
            return None

        account_id = str(row["metaapi_account_id"])
        try:
            region = await self._gateway.resolve_account_region(
                token=token,
                account_id=account_id,
            )
        except MetaApiGatewayError as exc:
            self._store_failed_snapshot(
                observed_at=observed_at,
                local_account_id=local_account_id,
                code=exc.code,
                stage="resolve_region",
            )
            return None

        return Phase0AccountAccess(
            local_account_id=local_account_id,
            metaapi_account_id=account_id,
            token=token,
            region=region,
        )

    async def _capture_snapshot(
        self,
        access: Phase0AccountAccess,
        observed_at: datetime,
    ) -> None:
        results: dict[str, object | None] = {
            "account": None,
            "positions": None,
            "orders": None,
            "price": None,
        }
        errors: list[dict[str, object]] = []
        reads = (
            (
                "account",
                self._gateway.read_account_information(
                    token=access.token,
                    account_id=access.metaapi_account_id,
                    region=access.region,
                ),
            ),
            (
                "positions",
                self._gateway.read_positions(
                    token=access.token,
                    account_id=access.metaapi_account_id,
                    region=access.region,
                ),
            ),
            (
                "orders",
                self._gateway.read_orders(
                    token=access.token,
                    account_id=access.metaapi_account_id,
                    region=access.region,
                ),
            ),
            (
                "price",
                self._gateway.read_symbol_price(
                    token=access.token,
                    account_id=access.metaapi_account_id,
                    region=access.region,
                    symbol=self._config.symbol,
                ),
            ),
        )
        for stage, operation in reads:
            try:
                results[stage] = await operation
            except MetaApiGatewayError as exc:
                errors.append({"stage": stage, "code": exc.code, "retryable": exc.retryable})
            except Exception as exc:
                errors.append(
                    {"stage": stage, "code": "phase0_unexpected_read_failure", "type": type(exc).__name__}
                )

        available_count = sum(value is not None for value in results.values())
        capture_status = (
            "complete" if not errors else "partial" if available_count else "failed"
        )
        account = results["account"] if isinstance(results["account"], dict) else {}
        price = results["price"] if isinstance(results["price"], dict) else {}
        positions = results["positions"] if isinstance(results["positions"], list) else []
        orders = results["orders"] if isinstance(results["orders"], list) else []

        payload = {
            "reference_user_id": self._config.reference_user_id,
            "mt5_account_id": access.local_account_id,
            "source": self._config.source,
            "symbol": self._config.symbol,
            "capture_status": capture_status,
            "captured_at": observed_at,
            "quote_time": _parse_time(price.get("time")),
            "bid": _decimal(price.get("bid")),
            "ask": _decimal(price.get("ask")),
            "balance": _decimal(account.get("balance")),
            "equity": _decimal(account.get("equity")),
            "margin": _decimal(account.get("margin")),
            "free_margin": _decimal(account.get("freeMargin")),
            "trade_allowed": (
                bool(account.get("tradeAllowed")) if "tradeAllowed" in account else None
            ),
            "positions": positions,
            "orders": orders,
            "capture_metadata": {
                "region": access.region,
                "errors": errors,
                "point_in_time_version": "phase0-market-snapshot-v1",
            },
        }
        first_error = errors[0] if errors else {}
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    INSERT INTO market_snapshots (
                        reference_user_id, mt5_account_id, source, symbol,
                        capture_status, captured_at, quote_time, bid, ask,
                        balance, equity, margin, free_margin, trade_allowed,
                        positions_json, orders_json, capture_metadata,
                        error_code, error_stage, snapshot_digest
                    ) VALUES (
                        :reference_user_id, :mt5_account_id, :source, :symbol,
                        :capture_status, :captured_at, :quote_time, :bid, :ask,
                        :balance, :equity, :margin, :free_margin, :trade_allowed,
                        CAST(:positions_json AS jsonb), CAST(:orders_json AS jsonb),
                        CAST(:capture_metadata AS jsonb), :error_code, :error_stage,
                        :snapshot_digest
                    )
                    """
                ),
                {
                    "reference_user_id": self._config.reference_user_id,
                    "mt5_account_id": access.local_account_id,
                    "source": self._config.source,
                    "symbol": self._config.symbol,
                    "capture_status": capture_status,
                    "captured_at": observed_at,
                    "quote_time": payload["quote_time"],
                    "bid": payload["bid"],
                    "ask": payload["ask"],
                    "balance": payload["balance"],
                    "equity": payload["equity"],
                    "margin": payload["margin"],
                    "free_margin": payload["free_margin"],
                    "trade_allowed": payload["trade_allowed"],
                    "positions_json": _canonical_json(positions),
                    "orders_json": _canonical_json(orders),
                    "capture_metadata": _canonical_json(payload["capture_metadata"]),
                    "error_code": first_error.get("code"),
                    "error_stage": first_error.get("stage"),
                    "snapshot_digest": _digest(payload),
                },
            )
            session.commit()

    async def _capture_closed_candles(
        self,
        access: Phase0AccountAccess,
        timeframe: str,
        observed_at: datetime,
    ) -> int:
        seconds = _TIMEFRAME_SECONDS[timeframe]
        payloads = await self._gateway.read_historical_candles(
            token=access.token,
            account_id=access.metaapi_account_id,
            region=access.region,
            symbol=self._config.symbol,
            timeframe=timeframe,
            limit=3,
        )
        inserted = 0
        with self._session_factory() as session:
            for payload in payloads:
                open_time = _parse_time(payload.get("time"))
                if open_time is None or open_time + timedelta(seconds=seconds) > observed_at:
                    continue
                values = {
                    "open": _decimal(payload.get("open")),
                    "high": _decimal(payload.get("high")),
                    "low": _decimal(payload.get("low")),
                    "close": _decimal(payload.get("close")),
                }
                if any(value is None for value in values.values()):
                    logger.warning("Phase 0 ignored malformed candle timeframe=%s", timeframe)
                    continue
                open_value = values["open"]
                high_value = values["high"]
                low_value = values["low"]
                close_value = values["close"]
                assert open_value is not None
                assert high_value is not None
                assert low_value is not None
                assert close_value is not None
                if high_value < max(open_value, close_value, low_value) or low_value > min(
                    open_value, close_value, high_value
                ):
                    logger.warning("Phase 0 ignored inconsistent OHLC timeframe=%s", timeframe)
                    continue
                result = session.execute(
                    text(
                        """
                        INSERT INTO market_candles (
                            source, symbol, timeframe, open_time, broker_time,
                            open, high, low, close, tick_volume, spread, volume,
                            observed_at, raw_payload
                        ) VALUES (
                            :source, :symbol, :timeframe, :open_time, :broker_time,
                            :open, :high, :low, :close, :tick_volume, :spread, :volume,
                            :observed_at, CAST(:raw_payload AS jsonb)
                        )
                        ON CONFLICT (source, symbol, timeframe, open_time) DO NOTHING
                        RETURNING id
                        """
                    ),
                    {
                        "source": self._config.source,
                        "symbol": str(payload.get("symbol") or self._config.symbol),
                        "timeframe": timeframe,
                        "open_time": open_time,
                        "broker_time": (
                            str(payload.get("brokerTime")) if payload.get("brokerTime") else None
                        ),
                        "open": open_value,
                        "high": high_value,
                        "low": low_value,
                        "close": close_value,
                        "tick_volume": (
                            int(payload["tickVolume"])
                            if isinstance(payload.get("tickVolume"), (int, float))
                            and not isinstance(payload.get("tickVolume"), bool)
                            else None
                        ),
                        "spread": _decimal(payload.get("spread")),
                        "volume": _decimal(payload.get("volume")),
                        "observed_at": observed_at,
                        "raw_payload": _canonical_json(payload),
                    },
                ).first()
                inserted += int(result is not None)
            session.commit()
        return inserted

    def record_event_observation(
        self,
        *,
        source: str,
        event_type: str,
        observed_at: datetime,
        raw_evidence: dict[str, object],
        external_event_id: str | None = None,
        occurred_at: datetime | None = None,
        available_at: datetime | None = None,
        revision_number: int = 0,
        headline: str | None = None,
        country: str | None = None,
        importance: int | None = None,
        scheduled: bool = False,
        actual_text: str | None = None,
        consensus_text: str | None = None,
        previous_text: str | None = None,
        revision_text: str | None = None,
    ) -> bool:
        """Append one external observation without overwriting later revisions."""
        observed = _utc(observed_at)
        digest_payload = {
            "source": source,
            "external_event_id": external_event_id,
            "event_type": event_type,
            "observed_at": observed,
            "occurred_at": _utc(occurred_at) if occurred_at else None,
            "available_at": _utc(available_at) if available_at else None,
            "revision_number": revision_number,
            "headline": headline,
            "country": country,
            "importance": importance,
            "scheduled": scheduled,
            "actual_text": actual_text,
            "consensus_text": consensus_text,
            "previous_text": previous_text,
            "revision_text": revision_text,
            "raw_evidence": raw_evidence,
        }
        observation_digest = _digest(digest_payload)
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    INSERT INTO market_event_observations (
                        source, external_event_id, event_type, observed_at,
                        occurred_at, available_at, revision_number, headline,
                        country, importance, scheduled, actual_text, consensus_text,
                        previous_text, revision_text, raw_evidence, observation_digest
                    ) VALUES (
                        :source, :external_event_id, :event_type, :observed_at,
                        :occurred_at, :available_at, :revision_number, :headline,
                        :country, :importance, :scheduled, :actual_text, :consensus_text,
                        :previous_text, :revision_text, CAST(:raw_evidence AS jsonb),
                        :observation_digest
                    )
                    ON CONFLICT (source, observation_digest) DO NOTHING
                    RETURNING id
                    """
                ),
                {
                    **{key: value for key, value in digest_payload.items() if key != "raw_evidence"},
                    "raw_evidence": _canonical_json(raw_evidence),
                    "observation_digest": observation_digest,
                },
            ).first()
            session.commit()
            return row is not None

    def _store_failed_snapshot(
        self,
        *,
        observed_at: datetime,
        local_account_id: UUID | None,
        code: str,
        stage: str,
    ) -> None:
        payload = {
            "reference_user_id": self._config.reference_user_id,
            "mt5_account_id": local_account_id,
            "source": self._config.source,
            "symbol": self._config.symbol,
            "capture_status": "failed",
            "captured_at": observed_at,
            "error_code": code,
            "error_stage": stage,
            "point_in_time_version": "phase0-market-snapshot-v1",
        }
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    INSERT INTO market_snapshots (
                        reference_user_id, mt5_account_id, source, symbol,
                        capture_status, captured_at, positions_json, orders_json,
                        capture_metadata, error_code, error_stage, snapshot_digest
                    ) VALUES (
                        :reference_user_id, :mt5_account_id, :source, :symbol,
                        'failed', :captured_at, '[]'::jsonb, '[]'::jsonb,
                        CAST(:capture_metadata AS jsonb), :error_code, :error_stage,
                        :snapshot_digest
                    )
                    """
                ),
                {
                    "reference_user_id": self._config.reference_user_id,
                    "mt5_account_id": local_account_id,
                    "source": self._config.source,
                    "symbol": self._config.symbol,
                    "captured_at": observed_at,
                    "capture_metadata": _canonical_json(
                        {"point_in_time_version": "phase0-market-snapshot-v1"}
                    ),
                    "error_code": code,
                    "error_stage": stage,
                    "snapshot_digest": _digest(payload),
                },
            )
            session.commit()


class Phase0MarketRecorderManager:
    """Small best-effort background scheduler for Phase 0 evidence capture."""

    def __init__(self, recorder: Phase0MarketRecorder, config: Phase0RecorderConfig) -> None:
        self._recorder = recorder
        self._config = config
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="phase0-market-recorder")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        next_slow_at = datetime.min.replace(tzinfo=UTC)
        while not self._stop.is_set():
            now = datetime.now(UTC)
            include_slow = now >= next_slow_at
            try:
                await self._recorder.capture_cycle(include_slow=include_slow, now=now)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Phase 0 recorder cycle failed without affecting trading")
            if include_slow:
                next_slow_at = now + timedelta(seconds=self._config.slow_poll_seconds)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._config.fast_poll_seconds,
                )
            except TimeoutError:
                pass
