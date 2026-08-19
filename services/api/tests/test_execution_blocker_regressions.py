from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import httpx
import pytest

import app.mt5_execution_day26 as day26_module
from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_trade_gateway import MetaApiMarketOrderResult, MetaApiTradeGateway
from app.mt5_execution_day26 import (
    Day26MappedPosition,
    Day26Mt5ExecutionService,
    _AccountInput,
    _PlannedPosition,
    _SignalInput,
)
from app.mt5_read_service_day23 import (
    Day23AccountState,
    Day23LiveState,
    Day23Mt5ReadService,
    Day23PriceState,
)
from app.trading_execution_canonical import MemberTradingExecutionService

OWNER = UUID("ea604df2-f8ee-47d1-bc51-f0078dbf160d")
SIGNAL = UUID("11111111-1111-4111-8111-111111111111")


class _TradeResponseGateway(MetaApiTradeGateway):
    def __init__(self, payload: dict[str, object]) -> None:
        super().__init__()
        self.payload = payload

    async def _request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return httpx.Response(200, json=self.payload)


@pytest.mark.parametrize(
    ("numeric_code", "string_code"),
    [
        (0, "ERR_NO_ERROR"),
        (10008, "TRADE_RETCODE_PLACED"),
        (10009, "TRADE_RETCODE_DONE"),
        (10010, "TRADE_RETCODE_DONE_PARTIAL"),
        (10025, "TRADE_RETCODE_NO_CHANGES"),
    ],
)
def test_documented_metaapi_success_codes_are_not_falsely_rejected(
    numeric_code: int,
    string_code: str,
) -> None:
    gateway = _TradeResponseGateway(
        {"numericCode": numeric_code, "stringCode": string_code, "orderId": "123"}
    )
    result = asyncio.run(
        gateway._trade_request(
            token="test-token",
            account_id="account",
            region="london",
            json_body={"actionType": "ORDER_TYPE_BUY"},
        )
    )
    assert result["numericCode"] == numeric_code


def test_actual_metaapi_rejection_still_fails() -> None:
    gateway = _TradeResponseGateway(
        {"numericCode": 10006, "stringCode": "TRADE_RETCODE_REJECT", "orderId": "123"}
    )
    with pytest.raises(MetaApiGatewayError, match="metaapi_trade_rejected"):
        asyncio.run(
            gateway._trade_request(
                token="test-token",
                account_id="account",
                region="london",
                json_body={"actionType": "ORDER_TYPE_BUY"},
            )
        )


def test_core_executor_default_cannot_chase_zone_for_five_minutes() -> None:
    service = Day26Mt5ExecutionService(
        session_factory=None,  # type: ignore[arg-type]
        cipher=None,  # type: ignore[arg-type]
        read_gateway=None,  # type: ignore[arg-type]
        margin_gateway=None,  # type: ignore[arg-type]
        trade_gateway=None,  # type: ignore[arg-type]
    )
    assert service._zone_wait_seconds == 0.0


class _LiveTruthCipher:
    def decrypt(self, value: bytes) -> str:
        assert value == b"encrypted"
        return "terminal-token-12345678901234567890"


class _LiveTruthGateway:
    async def resolve_account_region(self, **kwargs):  # noqa: ANN003
        return "london"

    async def read_account_information(self, **kwargs):  # noqa: ANN003
        return {
            "currency": "USD",
            "balance": 1000,
            "equity": 1000,
            "margin": 0,
            "freeMargin": 1000,
            "tradeAllowed": True,
        }

    async def read_positions(self, **kwargs):  # noqa: ANN003
        return []

    async def read_symbol_price(self, **kwargs):  # noqa: ANN003
        return {
            "symbol": "XAUUSD",
            "bid": 3999.9,
            "ask": 4000.0,
            "time": datetime.now(UTC).isoformat(),
            "profitTickValue": 1.0,
            "lossTickValue": 1.0,
        }


class _LiveTruthService(Day23Mt5ReadService):
    def _load_row(self, owner_user_id: UUID):  # type: ignore[override]
        assert owner_user_id == OWNER
        return {
            "id": UUID(int=5),
            "status": "disconnected",
            "metaapi_token_ciphertext": b"encrypted",
            "metaapi_account_id": "metaapi-account",
            "login": "25911913",
            "server": "VantageMarkets-Demo",
        }

    def _audit_success(self, state):  # noqa: ANN001
        return None

    def _audit_failure(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


def test_cached_disconnected_word_cannot_veto_a_successful_live_terminal_read() -> None:
    service = _LiveTruthService(
        session_factory=None,  # type: ignore[arg-type]
        cipher=_LiveTruthCipher(),  # type: ignore[arg-type]
        gateway=_LiveTruthGateway(),  # type: ignore[arg-type]
    )
    state = asyncio.run(service.read_owner_live_state(OWNER))
    assert state.account.trade_allowed is True
    assert state.execution_ready is True
    assert state.price.ask == 4000.0


def test_member_live_account_validation_does_not_require_cached_connected_status() -> None:
    MemberTradingExecutionService._validate_live_account_row(
        {
            "account_environment": "live",
            "login": "777001",
            "server": "VantageInternational-Live",
            "approved_login": "777001",
            "approved_server": "VantageInternational-Live",
        }
    )


def _live_state() -> Day23LiveState:
    now = datetime.now(UTC)
    return Day23LiveState(
        local_account_id=UUID(int=9),
        metaapi_account_id="metaapi-account",
        login_masked="****1913",
        server="VantageMarkets-Demo",
        region="london",
        read_at=now,
        account=Day23AccountState(
            currency="USD",
            balance=1000.0,
            equity=1000.0,
            margin=0.0,
            free_margin=1000.0,
            margin_level=None,
            leverage=500.0,
            trade_allowed=True,
        ),
        price=Day23PriceState(
            symbol="XAUUSD",
            bid=3999.9,
            ask=4000.0,
            buy_price=4000.0,
            sell_price=3999.9,
            quote_time=now,
            quote_age_seconds=0.1,
            available=True,
            stale=False,
            execution_ready=True,
            block_reason=None,
            profit_tick_value=1.0,
            loss_tick_value=1.0,
        ),
        positions=(),
        execution_ready=True,
        execution_block_reason=None,
    )


class _FakeDay23:
    def __init__(self, **kwargs):  # noqa: ANN003
        pass

    async def read_owner_live_state(self, owner_user_id: UUID) -> Day23LiveState:
        assert owner_user_id == OWNER
        return _live_state()

    executable_price = Day23Mt5ReadService.executable_price


class _VerifyReadGateway:
    def __init__(self) -> None:
        self.position_reads = 0

    async def read_symbol_specification(self, **kwargs):  # noqa: ANN003
        return {
            "tickSize": 0.01,
            "minVolume": 0.01,
            "maxVolume": 100.0,
            "volumeStep": 0.01,
        }

    async def read_positions(self, **kwargs):  # noqa: ANN003
        self.position_reads += 1
        if self.position_reads == 1:
            raise MetaApiGatewayError("metaapi_timeout", retryable=True)
        return []


class _VerifyMarginGateway:
    async def calculate_margin(self, **kwargs):  # noqa: ANN003
        return 10.0


class _VerifyTradeGateway:
    def __init__(self) -> None:
        self.place_calls = 0

    async def place_market_order(self, **kwargs):  # noqa: ANN003
        self.place_calls += 1
        return MetaApiMarketOrderResult(
            order_id="order-1",
            position_id="position-1",
            numeric_code=10009,
            string_code="TRADE_RETCODE_DONE",
        )


class _VerifyHarness(Day26Mt5ExecutionService):
    def __init__(self) -> None:
        self.read = _VerifyReadGateway()
        self.margin = _VerifyMarginGateway()
        self.trade = _VerifyTradeGateway()
        super().__init__(
            session_factory=None,  # type: ignore[arg-type]
            cipher=None,  # type: ignore[arg-type]
            read_gateway=self.read,  # type: ignore[arg-type]
            margin_gateway=self.margin,  # type: ignore[arg-type]
            trade_gateway=self.trade,  # type: ignore[arg-type]
        )
        self.signal = _SignalInput(
            signal_id=SIGNAL,
            symbol="XAUUSD",
            side="BUY",
            entry_low=Decimal("4000"),
            entry_high=Decimal("4000"),
            stop_loss=Decimal("3990"),
            take_profits=(Decimal("4010"),),
            has_open_runner=False,
            signal_requests_double_lot=False,
            source_revision_index=0,
            source_posted_at=datetime.now(UTC),
        )

    def _load_inputs(self, owner_user_id: UUID, signal_id: UUID):  # type: ignore[override]
        return self.signal, _AccountInput(UUID(int=10), "metaapi-account", b"cipher")

    def _decrypt_token(self, account):  # noqa: ANN001
        return "terminal-token-12345678901234567890"

    def _assert_signal_still_current(self, owner_user_id, signal):  # noqa: ANN001
        return None

    def _create_planned_positions(self, **kwargs):  # noqa: ANN003
        return (
            _PlannedPosition(UUID(int=11), 1, Decimal("4010"), "SS_000000000001_1"),
        )

    def _record_order_id(self, local_position_id, order_id):  # noqa: ANN001
        return None

    def _map_broker_positions(self, **kwargs):  # noqa: ANN003
        return (
            Day26MappedPosition(
                UUID(int=11),
                1,
                Decimal("4010"),
                Decimal("0.01"),
                "SS_000000000001_1",
                "order-1",
                "position-1",
                Decimal("4000"),
            ),
        )

    def _audit_blocked(self, **kwargs):  # noqa: ANN003
        return None

    def _record_execution_failure(self, **kwargs):  # noqa: ANN003
        return None

    def _audit_success(self, **kwargs):  # noqa: ANN003
        return None


def test_post_order_read_timeout_retries_verification_without_resubmitting_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(day26_module, "Day23Mt5ReadService", _FakeDay23)
    service = _VerifyHarness()
    result = asyncio.run(
        service.execute_owner_demo_signal(
            owner_user_id=OWNER,
            signal_id=SIGNAL,
            risk_percent="1",
            double_lot_approved=False,
        )
    )
    assert len(result.positions) == 1
    assert service.trade.place_calls == 1
    assert service.read.position_reads == 2
