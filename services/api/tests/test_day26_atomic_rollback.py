import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

import app.mt5_execution_day26 as day26_module
import app.mt5_execution_day26_atomic as atomic_module
from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_trade_gateway import MetaApiMarketOrderResult
from app.mt5_execution_day26 import (
    Day26ExecutionError,
    Day26MappedPosition,
    _AccountInput,
    _PlannedPosition,
    _SignalInput,
)
from app.mt5_execution_day26_atomic import AtomicDay26Mt5ExecutionService
from app.mt5_read_service_day23 import Day23AccountState, Day23LiveState, Day23PriceState

OWNER = UUID("ea604df2-f8ee-47d1-bc51-f0078dbf160d")
SIGNAL = UUID("3e7ec830-9a9a-42df-b908-b331863ff6a4")


def _live_state(price: float = 4000.0) -> Day23LiveState:
    now = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
    return Day23LiveState(
        local_account_id=UUID(int=99),
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
            bid=price,
            ask=price,
            buy_price=price,
            sell_price=price,
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
    def __init__(self, **_: object) -> None:
        pass

    async def read_owner_live_state(self, owner_user_id: UUID) -> Day23LiveState:
        assert owner_user_id == OWNER
        return _live_state()


class _ReadGateway:
    def __init__(self) -> None:
        self.positions: list[dict[str, object]] = []

    async def read_symbol_specification(self, **_: object) -> dict[str, object]:
        return {
            "tickSize": 0.01,
            "minVolume": 0.01,
            "maxVolume": 100.0,
            "volumeStep": 0.01,
        }

    async def read_positions(self, **_: object) -> list[dict[str, object]]:
        return list(self.positions)


class _MarginGateway:
    async def calculate_margin(self, **_: object) -> float:
        return 50.0


class _FailingTradeGateway:
    def __init__(self, *, fail_on: int = 2, close_fails: bool = False) -> None:
        self.fail_on = fail_on
        self.close_fails = close_fails
        self.place_calls: list[dict[str, object]] = []
        self.close_calls: list[str] = []

    async def place_market_order(self, **kwargs: object) -> MetaApiMarketOrderResult:
        self.place_calls.append(kwargs)
        call_no = len(self.place_calls)
        if call_no == self.fail_on:
            raise MetaApiGatewayError("metaapi_trade_rejected")
        return MetaApiMarketOrderResult(
            order_id=f"order-{call_no}",
            position_id=f"broker-{call_no}",
            numeric_code=10009,
            string_code="TRADE_RETCODE_DONE",
        )

    async def close_position(self, *, position_id: str, **_: object) -> None:
        self.close_calls.append(position_id)
        if self.close_fails:
            raise MetaApiGatewayError("metaapi_trade_rejected")


class _AtomicHarness(AtomicDay26Mt5ExecutionService):
    def __init__(self, *, fail_on: int = 2, close_fails: bool = False) -> None:
        self.read = _ReadGateway()
        self.margin = _MarginGateway()
        self.trade = _FailingTradeGateway(fail_on=fail_on, close_fails=close_fails)
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
            entry_price=Decimal("4000"),
            stop_loss=Decimal("3990"),
            take_profits=(Decimal("4010"), Decimal("4020"), Decimal("4030")),
            signal_requests_double_lot=False,
        )
        self.order_ids: dict[UUID, str] = {}
        self.rollback_persisted: tuple[dict[UUID, str], dict[UUID, str], str] | None = None
        self.rollback_audit: dict[str, object] | None = None
        self.planned = tuple(
            _PlannedPosition(
                local_position_id=UUID(int=index),
                tp_index=index,
                take_profit=tp,
                client_id=f"SS_{index:012d}_{index}",
            )
            for index, tp in enumerate(self.signal.take_profits, start=1)
        )

    def _load_inputs(self, owner_user_id: UUID, signal_id: UUID):  # type: ignore[override]
        assert owner_user_id == OWNER and signal_id == SIGNAL
        return self.signal, _AccountInput(UUID(int=100), "metaapi-account", b"cipher")

    def _decrypt_token(self, account: _AccountInput) -> str:  # type: ignore[override]
        return "test-token-with-terminal-access"

    def _create_planned_positions(self, **_: object):  # type: ignore[override]
        return self.planned

    def _record_order_id(self, local_position_id: UUID, order_id: str) -> None:
        self.order_ids[local_position_id] = order_id
        item = next(row for row in self.planned if row.local_position_id == local_position_id)
        self.read.positions.append(
            {
                "id": f"broker-{item.tp_index}",
                "clientId": item.client_id,
                "symbol": "XAUUSD",
                "type": "POSITION_TYPE_BUY",
                "volume": 0.01,
                "stopLoss": 3990.0,
                "takeProfit": float(item.take_profit),
                "openPrice": 4000.0,
            }
        )

    def _map_broker_positions(self, *, signal, sizing, planned, order_ids, **_):  # type: ignore[override]
        return tuple(
            Day26MappedPosition(
                local_position_id=item.local_position_id,
                tp_index=item.tp_index,
                take_profit=item.take_profit,
                volume=sizing.volume,
                client_id=item.client_id,
                broker_order_id=order_ids[item.client_id],
                broker_position_id=f"broker-{item.tp_index}",
                broker_open_price=signal.entry_price,
            )
            for item in planned
        )

    def _audit(self, **_: object) -> None:  # type: ignore[override]
        return None

    def _rollback_rows(self, owner_user_id: UUID, signal_id: UUID) -> list[dict]:
        assert owner_user_id == OWNER and signal_id == SIGNAL
        return [
            {
                "id": item.local_position_id,
                "broker_client_id": item.client_id,
                "broker_order_id": self.order_ids.get(item.local_position_id),
                "broker_position_id": None,
                "status": "planned",
            }
            for item in self.planned
        ]

    def _rollback_account(self, owner_user_id: UUID) -> tuple[str, str]:
        assert owner_user_id == OWNER
        return "metaapi-account", "test-token-with-terminal-access"

    def _mark_unsubmitted_failed(self, local_rows: list[dict], original_code: str) -> None:
        return None

    def _mark_rollback_unresolved(self, local_rows: list[dict], original_code: str) -> None:
        return None

    def _persist_rollback_state(self, *, identified, closed, original_code, **_):
        self.rollback_persisted = (identified, closed, original_code)

    def _audit_rollback(self, **kwargs: object) -> None:
        self.rollback_audit = kwargs


def _patch_day23(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(day26_module, "Day23Mt5ReadService", _FakeDay23)
    monkeypatch.setattr(atomic_module, "Day23Mt5ReadService", _FakeDay23)


def test_second_order_failure_closes_first_position(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_day23(monkeypatch)
    service = _AtomicHarness(fail_on=2)

    with pytest.raises(Day26ExecutionError, match="metaapi_trade_rejected"):
        asyncio.run(
            service.execute_owner_demo_signal(
                owner_user_id=OWNER,
                signal_id=SIGNAL,
                risk_percent="1",
                double_lot_approved=False,
            )
        )

    assert len(service.trade.place_calls) == 2
    assert service.trade.close_calls == ["broker-1"]
    assert service.rollback_persisted is not None
    identified, closed, original_code = service.rollback_persisted
    assert identified == {UUID(int=1): "broker-1"}
    assert closed == {UUID(int=1): "broker-1"}
    assert original_code == "metaapi_trade_rejected"
    assert service.rollback_audit is not None
    assert service.rollback_audit["submitted_count"] == 1
    assert service.rollback_audit["closed_count"] == 1
    assert service.rollback_audit["unresolved_count"] == 0


def test_third_order_failure_closes_both_prior_positions(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_day23(monkeypatch)
    service = _AtomicHarness(fail_on=3)

    with pytest.raises(Day26ExecutionError, match="metaapi_trade_rejected"):
        asyncio.run(
            service.execute_owner_demo_signal(
                owner_user_id=OWNER,
                signal_id=SIGNAL,
                risk_percent="1",
                double_lot_approved=False,
            )
        )

    assert service.trade.close_calls == ["broker-2", "broker-1"]
    assert service.rollback_audit is not None
    assert service.rollback_audit["submitted_count"] == 2
    assert service.rollback_audit["closed_count"] == 2
    assert service.rollback_audit["unresolved_count"] == 0


def test_rollback_failure_escalates_distinctly(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_day23(monkeypatch)
    service = _AtomicHarness(fail_on=2, close_fails=True)

    with pytest.raises(Day26ExecutionError, match="day26_partial_execution_rollback_failed"):
        asyncio.run(
            service.execute_owner_demo_signal(
                owner_user_id=OWNER,
                signal_id=SIGNAL,
                risk_percent="1",
                double_lot_approved=False,
            )
        )

    assert service.trade.close_calls == ["broker-1"]
    assert service.rollback_audit is not None
    assert service.rollback_audit["unresolved_count"] == 1
