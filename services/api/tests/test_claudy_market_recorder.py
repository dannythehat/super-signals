import asyncio
import inspect
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

import app.claudy_market_recorder as recorder_module
import app.claudy_market_repository as repository_module
from app.claudy_market_recorder import (
    CaptureResult,
    ClaudyMarketRecorderManager,
    ClaudyMarketRecorderService,
    _closed_candle,
    _session_code,
)


class FakeCipher:
    def decrypt(self, ciphertext: bytes) -> str:
        assert ciphertext == b"encrypted"
        return "super-secret-metaapi-token"


class InMemoryRepository:
    def __init__(
        self,
        *,
        account: dict[str, object] | None = None,
        event_ids: list[UUID] | None = None,
    ) -> None:
        self.account = account
        self.event_ids = list(event_ids or [])
        self.candles: dict[
            tuple[str, str, str, datetime],
            list[tuple[UUID, dict[str, object]]],
        ] = {}
        self.snapshots: list[dict[str, object]] = []

    def load_reference_demo_account(self, owner_user_id: UUID):
        return self.account

    def provider_state_summary(self) -> dict[str, int]:
        return {"testing": 2, "live": 1, "paused": 1}

    def event_observation_ids_known_at(self, *, captured_at: datetime) -> list[UUID]:
        return list(self.event_ids)

    def store_candle(self, candle: dict[str, object]) -> tuple[UUID, int, bool]:
        key = (
            str(candle["source"]),
            str(candle["symbol"]),
            str(candle["timeframe"]),
            candle["open_time_utc"],
        )
        assert isinstance(key[3], datetime)
        revisions = self.candles.setdefault(key, [])
        for index, (candle_id, existing) in enumerate(revisions, start=1):
            if existing["payload_digest"] == candle["payload_digest"]:
                return candle_id, index, False
        candle_id = uuid4()
        revisions.append((candle_id, dict(candle)))
        return candle_id, len(revisions), True

    def latest_candle_ids(self, *, symbol: str) -> dict[str, UUID]:
        latest: dict[str, tuple[datetime, int, UUID]] = {}
        for (_, candle_symbol, timeframe, open_time), revisions in self.candles.items():
            if candle_symbol != symbol:
                continue
            revision_index = len(revisions)
            candle_id = revisions[-1][0]
            current = latest.get(timeframe)
            if current is None or (open_time, revision_index) > (current[0], current[1]):
                latest[timeframe] = (open_time, revision_index, candle_id)
        return {timeframe: value[2] for timeframe, value in latest.items()}

    def store_snapshot(self, snapshot: dict[str, object]) -> UUID:
        self.snapshots.append(dict(snapshot))
        return uuid4()

    @property
    def candle_revision_count(self) -> int:
        return sum(len(revisions) for revisions in self.candles.values())


class FakeGateway:
    def __init__(self, captured_at: datetime, *, close_offset: float = 0) -> None:
        self.captured_at = captured_at
        self.close_offset = close_offset
        self.calls: list[str] = []
        self.candle_limits: dict[str, int] = {}

    async def resolve_account_region(self, *, token: str, account_id: str) -> str:
        assert token == "super-secret-metaapi-token"
        self.calls.append("region")
        return "london"

    async def read_account_information(self, **kwargs):
        self.calls.append("account")
        raise AssertionError("Phase 0 must not call the account-information endpoint")

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
        raise AssertionError("Phase 0 must not call the open-orders endpoint")

    async def read_symbol_price(self, **kwargs):
        self.calls.append("quote")
        return {
            "symbol": "XAUUSD",
            "bid": 4358.8,
            "ask": 4359.0,
            "time": (self.captured_at - timedelta(seconds=2)).isoformat(),
        }

    async def read_historical_candles(self, *, timeframe: str, limit: int, **kwargs):
        self.calls.append(f"candles:{timeframe}")
        self.candle_limits[timeframe] = limit
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
                "high": 4360 + self.close_offset,
                "low": 4290,
                "close": 4350 + self.close_offset,
                "tickVolume": 100,
                "spread": 20,
                "volume": 5,
            }

        return [row(forming_open), row(closed_open)]


def _service(
    repository: InMemoryRepository,
    now: datetime,
    *,
    close_offset: float = 0,
    gateway: FakeGateway | None = None,
) -> ClaudyMarketRecorderService:
    return ClaudyMarketRecorderService(
        reference_user_id=uuid4(),
        repository=repository,  # type: ignore[arg-type]
        cipher=FakeCipher(),  # type: ignore[arg-type]
        gateway=gateway or FakeGateway(now, close_offset=close_offset),  # type: ignore[arg-type]
        market_closed_stale_seconds=300,
    )


def test_capture_records_only_closed_candles_sanitizes_state_and_links_known_events() -> None:
    now = datetime(2026, 8, 15, 5, 30, tzinfo=UTC)
    known_event_ids = [uuid4(), uuid4()]
    repository = InMemoryRepository(
        account={
            "metaapi_account_id": "account-1",
            "metaapi_token_ciphertext": b"encrypted",
            "account_environment": "demo",
        },
        event_ids=known_event_ids,
    )
    gateway = FakeGateway(now)

    result = asyncio.run(
        _service(repository, now, gateway=gateway).capture_once(now=now)
    )

    assert result.status == "complete"
    assert result.market_open is True
    assert result.stored_candles == 6
    assert repository.candle_revision_count == 6
    assert len(repository.snapshots) == 1
    assert "account" not in gateway.calls
    assert "orders" not in gateway.calls
    assert gateway.candle_limits == {
        "1m": 20,
        "5m": 10,
        "15m": 5,
        "1h": 3,
        "4h": 3,
        "1d": 3,
    }

    snapshot = repository.snapshots[0]
    assert snapshot["latest_m1_id"] is not None
    assert snapshot["latest_m5_id"] is not None
    assert snapshot["latest_m15_id"] is not None
    assert snapshot["latest_h1_id"] is not None
    assert snapshot["latest_h4_id"] is not None
    assert snapshot["latest_d1_id"] is not None
    assert snapshot["terminal_trade_allowed"] is None
    # Orders are never read in Phase 0-lite, so the column must stay unknown (NULL).
    # An empty array would falsely claim we looked and found no open orders.
    assert snapshot["order_state_json"] is None
    assert json.loads(str(snapshot["event_observation_ids_json"])) == [
        str(item) for item in known_event_ids
    ]

    availability = json.loads(str(snapshot["data_availability_json"]))
    assert availability["external_events"] == "point_in_time_linked"
    assert availability["external_event_observation_count"] == 2
    assert availability["account_information"] == "not_captured_phase0_cost_control"
    assert availability["orders"] == "not_captured_phase0_pending_unsupported"

    stored_json = json.dumps(snapshot, default=str)
    assert "super-secret-metaapi-token" not in stored_json
    assert "must-not-store" not in stored_json
    assert '"decision_engine_enabled":false' in str(snapshot["claudy_state_json"])
    assert '"telegram_output_enabled":false' in str(snapshot["claudy_state_json"])
    assert '"status":"not_configured_phase0_lite"' in str(snapshot["cross_market_state_json"])


def test_duplicate_polling_keeps_same_candle_revisions_and_adds_no_new_candles() -> None:
    now = datetime(2026, 8, 15, 5, 30, tzinfo=UTC)
    repository = InMemoryRepository(
        account={
            "metaapi_account_id": "account-1",
            "metaapi_token_ciphertext": b"encrypted",
        }
    )
    service = _service(repository, now)

    first = asyncio.run(service.capture_once(now=now))
    first_ids = repository.latest_candle_ids(symbol="XAUUSD")
    second = asyncio.run(service.capture_once(now=now))
    second_ids = repository.latest_candle_ids(symbol="XAUUSD")

    assert first.stored_candles == 6
    assert second.stored_candles == 0
    assert repository.candle_revision_count == 6
    assert first_ids == second_ids
    assert len(repository.snapshots) == 2


def test_corrected_closed_candle_becomes_new_revision_and_snapshot_uses_it() -> None:
    now = datetime(2026, 8, 15, 5, 30, tzinfo=UTC)
    repository = InMemoryRepository(
        account={
            "metaapi_account_id": "account-1",
            "metaapi_token_ciphertext": b"encrypted",
        }
    )

    first = asyncio.run(_service(repository, now).capture_once(now=now))
    first_ids = repository.latest_candle_ids(symbol="XAUUSD")
    second = asyncio.run(
        _service(repository, now, close_offset=1).capture_once(now=now)
    )
    second_ids = repository.latest_candle_ids(symbol="XAUUSD")

    assert first.stored_candles == 6
    assert second.stored_candles == 6
    assert repository.candle_revision_count == 12
    assert first_ids.keys() == second_ids.keys()
    assert all(first_ids[key] != second_ids[key] for key in first_ids)
    assert repository.snapshots[-1]["latest_m5_id"] == second_ids["5m"]


def test_missing_demo_account_is_recorded_as_unavailable_evidence() -> None:
    now = datetime(2026, 8, 15, 5, 30, tzinfo=UTC)
    event_id = uuid4()
    repository = InMemoryRepository(account=None, event_ids=[event_id])

    result = asyncio.run(_service(repository, now).capture_once(now=now))

    assert result.status == "unavailable"
    assert result.market_open is False
    assert result.snapshot_id is not None
    assert len(repository.snapshots) == 1
    snapshot = repository.snapshots[0]
    availability = json.loads(str(snapshot["data_availability_json"]))
    assert availability["reference_demo_account"] == "not_configured"
    assert availability["external_events"] == "point_in_time_linked"
    assert json.loads(str(snapshot["event_observation_ids_json"])) == [str(event_id)]


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


class OpenMarketService:
    async def capture_once(self, **kwargs):
        return CaptureResult(uuid4(), "complete", True, 0)


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


def test_default_open_market_cadence_is_sixty_seconds() -> None:
    delays: list[float] = []

    async def stop_after_sleep(delay: float) -> None:
        delays.append(delay)
        raise asyncio.CancelledError

    manager = ClaudyMarketRecorderManager(
        OpenMarketService(),  # type: ignore[arg-type]
        sleep=stop_after_sleep,
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(manager._run())

    assert delays == [60]


class RecordingTimeframeService:
    def __init__(self) -> None:
        self.timeframe_calls: list[tuple[str, ...]] = []

    async def capture_once(self, *, timeframes: tuple[str, ...], now: datetime):
        self.timeframe_calls.append(timeframes)
        return CaptureResult(uuid4(), "complete", True, 0)


def test_fast_cycles_run_alone_and_the_slow_set_re_enters_after_five_minutes() -> None:
    """The two cadences must be real, not collapsed into one.

    With the shipped defaults the fast set runs every 60s and the slow set may only
    re-enter once 300s have passed. If slow_poll ever drops to poll_seconds this test
    fails, because every cycle would read all six timeframes.
    """
    service = RecordingTimeframeService()
    clock = {"now": datetime(2026, 8, 15, 5, 0, tzinfo=UTC)}
    delays: list[float] = []

    async def advance(delay: float) -> None:
        delays.append(delay)
        clock["now"] += timedelta(seconds=delay)
        if len(delays) >= 6:
            raise asyncio.CancelledError

    manager = ClaudyMarketRecorderManager(
        service,  # type: ignore[arg-type]
        poll_seconds=60,
        slow_poll_seconds=300,
        market_closed_backoff_seconds=900,
        sleep=advance,
        clock=lambda: clock["now"],
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(manager._run())

    assert delays == [60, 60, 60, 60, 60, 60]
    assert service.timeframe_calls == [
        recorder_module.ALL_TIMEFRAMES,
        recorder_module.FAST_TIMEFRAMES,
        recorder_module.FAST_TIMEFRAMES,
        recorder_module.FAST_TIMEFRAMES,
        recorder_module.FAST_TIMEFRAMES,
        recorder_module.ALL_TIMEFRAMES,
    ]


def test_default_closed_market_backoff_is_fifteen_minutes() -> None:
    delays: list[float] = []

    async def stop_after_sleep(delay: float) -> None:
        delays.append(delay)
        raise asyncio.CancelledError

    manager = ClaudyMarketRecorderManager(
        ClosedMarketService(),  # type: ignore[arg-type]
        sleep=stop_after_sleep,
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(manager._run())

    assert delays == [900]


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
