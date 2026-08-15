import asyncio
from datetime import UTC, datetime, timedelta
import inspect
import json
from uuid import UUID, uuid4

import pytest

import app.claudy_market_recorder as recorder_module
import app.claudy_market_repository as repository_module
from app.claudy_market_recorder import (
    ClaudyMarketRecorderManager,
    ClaudyMarketRecorderService,
    CaptureResult,
    _closed_candle,
    _session_code,
)


class FakeCipher:
    def decrypt(self, ciphertext: bytes) -> str:
        assert ciphertext == b"encrypted"
        return "super-secret-metaapi-token"


class InMemoryRepository:
    def __init__(self, *, account: dict[str, object] | None = None) -> None:
        self.account = account
        self.candles: dict[tuple[str, str, str, datetime], tuple[UUID, dict[str, object]]] = {}
        self.snapshots: list[dict[str, object]] = []

    def load_reference_demo_account(self, owner_user_id: UUID):
        return self.account

    def provider_state_summary(self) -> dict[str, int]:
        return {"testing": 2, "live": 1, "paused": 1}

    def store_candle(self, candle: dict[str, object]) -> UUID:
        key = (
            str(candle["source"]),
            str(candle["symbol"]),
            str(candle["timeframe"]),
            candle["open_time_utc"],
        )
        assert isinstance(key[3], datetime)
        existing = self.candles.get(key)
        if existing is not None:
            return existing[0]
        candle_id = uuid4()
        self.candles[key] = (candle_id, dict(candle))
        return candle_id

    def latest_candle_ids(self, *, symbol: str) -> dict[str, UUID]:
        latest: dict[str, tuple[datetime, UUID]] = {}
        for (_, candle_symbol, timeframe, open_time), (candle_id, _) in self.candles.items():
            if candle_symbol != symbol:
                continue
            current = latest.get(timeframe)
            if current is None or open_time > current[0]:
                latest[timeframe] = (open_time, candle_id)
        return {timeframe: value[1] for timeframe, value in latest.items()}

    def store_snapshot(self, snapshot: dict[str, object]) -> UUID:
        self.snapshots.append(dict(snapshot))
        return uuid4()


class FakeGateway:
    def __init__(self, captured_at: datetime) -> None:
        self.captured_at = captured_at
        self.calls: list[str] = []

    async def resolve_account_region(self, *, token: str, account_id: str) -> str:
        assert token == "super-secret-metaapi-token"
        self.calls.append("region")
        return "london"

    async def read_account_information(self, **kwargs):
        self.calls.append("account")
        return {"tradeAllowed": True, "balance": 999999, "password": "must-not-store"}

    async def read_positions(self, **kwargs):
        self.calls.append("positions")
        return [
            {
                "id": "position-1",
                "symbol": "XAUUSD",
                "type": "POSITION_TYPE_BUY",
                "volume": 0.01,
                "openPrice": 4349.42,
                "currentPrice": 4359.0,
                "stopLoss": 4342,
                "takeProfit": None,
                "profit": 9.58,
                "clientId": "safe-client-id",
                "password": "must-not-store",
                "comment": "not-needed",
            }
        ]

    async def read_orders(self, **kwargs):
        self.calls.append("orders")
        return [
            {
                "id": "order-1",
                "symbol": "XAUUSD",
                "type": "ORDER_TYPE_BUY_LIMIT",
                "state": "ORDER_STATE_PLACED",
                "volume": 0.01,
                "currentVolume": 0.01,
                "openPrice": 4330,
                "clientId": "safe-order-client",
                "auth-token": "must-not-store",
            }
        ]

    async def read_symbol_price(self, **kwargs):
        self.calls.append("quote")
        return {
            "symbol": "XAUUSD",
            "bid": 4358.8,
            "ask": 4359.0,
            "time": (self.captured_at - timedelta(seconds=2)).isoformat(),
        }

    async def read_historical_candles(self, *, timeframe: str, **kwargs):
        self.calls.append(f"candles:{timeframe}")
        seconds = {
            "1m": 60,
            "5m": 300,
            "15m": 900,
            "1h": 3600,
            "4h": 14400,
            "1d": 86400,
        }[timeframe]
        closed_open = self.captured_at - timedelta(seconds=seconds * 2)
        forming_open = self.captured_at - timedelta(seconds=max(1, seconds // 2))

        def row(open_time: datetime):
            return {
                "symbol": "XAUUSD",
                "timeframe": timeframe,
                "time": open_time.isoformat(),
                "brokerTime": "2026-08-15 00:00:00.000",
                "open": 4300,
                "high": 4360,
                "low": 4290,
                "close": 4350,
                "tickVolume": 100,
                "spread": 20,
                "volume": 5,
            }

        return [row(forming_open), row(closed_open)]


def _service(repository: InMemoryRepository, now: datetime) -> ClaudyMarketRecorderService:
    return ClaudyMarketRecorderService(
        reference_user_id=uuid4(),
        repository=repository,  # type: ignore[arg-type]
        cipher=FakeCipher(),  # type: ignore[arg-type]
        gateway=FakeGateway(now),  # type: ignore[arg-type]
        market_closed_stale_seconds=300,
    )


def test_capture_records_only_closed_candles_and_sanitizes_broker_state() -> None:
    now = datetime(2026, 8, 15, 5, 30, tzinfo=UTC)
    repository = InMemoryRepository(
        account={
            "metaapi_account_id": "account-1",
            "metaapi_token_ciphertext": b"encrypted",
            "account_environment": "demo",
        }
    )

    result = asyncio.run(_service(repository, now).capture_once(now=now))

    assert result.status == "complete"
    assert result.market_open is True
    assert result.broker_trade_action_created is False
    assert len(repository.candles) == 6
    assert len(repository.snapshots) == 1

    snapshot = repository.snapshots[0]
    assert snapshot["latest_m1_id"] is not None
    assert snapshot["latest_m5_id"] is not None
    assert snapshot["latest_m15_id"] is not None
    assert snapshot["latest_h1_id"] is not None
    assert snapshot["latest_h4_id"] is not None
    assert snapshot["latest_d1_id"] is not None

    stored_json = json.dumps(snapshot, default=str)
    assert "super-secret-metaapi-token" not in stored_json
    assert "must-not-store" not in stored_json
    assert '"decision_engine_enabled":false' in str(snapshot["claudy_state_json"])
    assert '"telegram_output_enabled":false' in str(snapshot["claudy_state_json"])
    assert '"status":"not_configured_phase0_lite"' in str(snapshot["cross_market_state_json"])


def test_duplicate_polling_keeps_immutable_candle_identity() -> None:
    now = datetime(2026, 8, 15, 5, 30, tzinfo=UTC)
    repository = InMemoryRepository(
        account={
            "metaapi_account_id": "account-1",
            "metaapi_token_ciphertext": b"encrypted",
        }
    )
    service = _service(repository, now)

    asyncio.run(service.capture_once(now=now))
    first_ids = {key: value[0] for key, value in repository.candles.items()}
    asyncio.run(service.capture_once(now=now))
    second_ids = {key: value[0] for key, value in repository.candles.items()}

    assert len(repository.candles) == 6
    assert first_ids == second_ids
    assert len(repository.snapshots) == 2


def test_missing_demo_account_is_recorded_as_unavailable_evidence() -> None:
    now = datetime(2026, 8, 15, 5, 30, tzinfo=UTC)
    repository = InMemoryRepository(account=None)

    result = asyncio.run(_service(repository, now).capture_once(now=now))

    assert result.status == "unavailable"
    assert result.market_open is False
    assert result.snapshot_id is not None
    assert len(repository.snapshots) == 1
    availability = json.loads(str(repository.snapshots[0]["data_availability_json"]))
    assert availability["reference_demo_account"] == "not_configured"


def test_forming_candle_is_never_written_as_closed_history() -> None:
    now = datetime(2026, 8, 15, 5, 30, tzinfo=UTC)
    forming = {
        "symbol": "XAUUSD",
        "timeframe": "5m",
        "time": (now - timedelta(minutes=2)).isoformat(),
        "open": 1,
        "high": 2,
        "low": 0.5,
        "close": 1.5,
    }
    closed = dict(forming)
    closed["time"] = (now - timedelta(minutes=10)).isoformat()

    assert _closed_candle(forming, expected_timeframe="5m", captured_at=now) is None
    assert _closed_candle(closed, expected_timeframe="5m", captured_at=now) is not None


def test_session_label_is_dst_aware_while_storage_remains_utc() -> None:
    summer = datetime(2026, 7, 15, 12, 30, tzinfo=UTC)
    winter = datetime(2026, 1, 15, 12, 30, tzinfo=UTC)

    assert _session_code(summer) == "london_new_york_overlap"
    assert _session_code(winter) == "london"


class FailingService:
    async def capture_once(self, **kwargs):
        raise RuntimeError("recorder failure")


class ClosedMarketService:
    async def capture_once(self, **kwargs):
        return CaptureResult(uuid4(), "complete", False, 0)


def test_background_manager_failure_is_isolated_and_backs_off() -> None:
    delays: list[float] = []

    async def stop_after_sleep(delay: float) -> None:
        delays.append(delay)
        raise asyncio.CancelledError

    manager = ClaudyMarketRecorderManager(
        FailingService(),  # type: ignore[arg-type]
        poll_seconds=60,
        slow_poll_seconds=300,
        market_closed_backoff_seconds=300,
        sleep=stop_after_sleep,
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(manager._run())

    assert delays == [300]


def test_market_closed_uses_backoff_instead_of_hammering_metaapi() -> None:
    delays: list[float] = []

    async def stop_after_sleep(delay: float) -> None:
        delays.append(delay)
        raise asyncio.CancelledError

    manager = ClaudyMarketRecorderManager(
        ClosedMarketService(),  # type: ignore[arg-type]
        poll_seconds=60,
        slow_poll_seconds=300,
        market_closed_backoff_seconds=300,
        sleep=stop_after_sleep,
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(manager._run())

    assert delays == [300]


def test_phase0_recorder_has_no_execution_or_telegram_mutation_path() -> None:
    source = inspect.getsource(recorder_module) + inspect.getsource(repository_module)

    assert "MetaApiTradeGateway" not in source
    assert "INSERT INTO signals" not in source
    assert "UPDATE signals" not in source
    assert "INSERT INTO positions" not in source
    assert "UPDATE positions" not in source
    assert "TelegramListener" not in source
    assert "TelegramPublisher" not in source
    assert "send_message(" not in source
    assert "telegram_publisher" not in source
    assert "account_environment='demo'" in inspect.getsource(
        repository_module.ClaudyMarketRepository.load_reference_demo_account
    )
