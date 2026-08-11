"""One-shot pure-code acceptance probe for Day 26 execution.

This probe never contacts MetaAPI and never writes to the database. It exercises the
real Day 23 -> Day 24 -> Day 25 -> Day 26 orchestration using deterministic fake
broker gateways, including compensating rollback after partial multi-TP submission.
Enable temporarily with SUPER_SIGNALS_DAY26_CODE_PROBE=1 during a Render deploy.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import app.mt5_execution_day26 as day26_module
import app.mt5_execution_day26_atomic as atomic_module
from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_trade_gateway import MetaApiMarketOrderResult
from app.mt5_execution_day26 import (
    Day26ExecutionError,
    Day26MappedPosition,
    Day26Mt5ExecutionService,
    _AccountInput,
    _PlannedPosition,
    _SignalInput,
)
from app.mt5_execution_day26_atomic import AtomicDay26Mt5ExecutionService
from app.mt5_read_service_day23 import Day23AccountState, Day23LiveState, Day23PriceState

logger = logging.getLogger(__name__)
_OWNER = UUID("ea604df2-f8ee-47d1-bc51-f0078dbf160d")
_SIGNAL = UUID("3e7ec830-9a9a-42df-b908-b331863ff6a4")


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise RuntimeError(f"day26_code_probe_failed:{code}")


def _live_state(price: float = 4000.0) -> Day23LiveState:
    now = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
    return Day23LiveState(
        local_account_id=UUID(int=99),
        metaapi_account_id="probe-metaapi-account",
        login_masked="****0000",
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
        _require(owner_user_id == _OWNER, "owner_mismatch")
        return _live_state()


class _ReadGateway:
    def __init__(self) -> None:
        self.positions: list[dict[str, object]] = []
        self.position_reads = 0

    async def read_symbol_specification(self, **_: object) -> dict[str, object]:
        return {
            "tickSize": 0.01,
            "minVolume": 0.01,
            "maxVolume": 100.0,
            "volumeStep": 0.01,
        }

    async def read_positions(self, **_: object) -> list[dict[str, object]]:
        self.position_reads += 1
        return list(self.positions)


class _MarginGateway:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def calculate_margin(self, **kwargs: object) -> float:
        self.calls.append(kwargs)
        return 50.0


class _TradeGateway:
    def __init__(self, *, fail_on: int | None = None, close_fails: bool = False) -> None:
        self.fail_on = fail_on
        self.close_fails = close_fails
        self.place_calls: list[dict[str, object]] = []
        self.close_calls: list[str] = []

    async def place_market_order(self, **kwargs: object) -> MetaApiMarketOrderResult:
        self.place_calls.append(kwargs)
        call_no = len(self.place_calls)
        if self.fail_on == call_no:
            raise MetaApiGatewayError("metaapi_trade_rejected")
        return MetaApiMarketOrderResult(
            order_id=f"probe-order-{call_no}",
            position_id=f"probe-position-{call_no}",
            numeric_code=10009,
            string_code="TRADE_RETCODE_DONE",
        )

    async def close_position(self, *, position_id: str, **_: object) -> None:
        self.close_calls.append(position_id)
        if self.close_fails:
            raise MetaApiGatewayError("metaapi_trade_rejected")


class _SuccessHarness(Day26Mt5ExecutionService):
    def __init__(self) -> None:
        self.read = _ReadGateway()
        self.margin = _MarginGateway()
        self.trade = _TradeGateway()
        super().__init__(
            session_factory=None,  # type: ignore[arg-type]
            cipher=None,  # type: ignore[arg-type]
            read_gateway=self.read,  # type: ignore[arg-type]
            margin_gateway=self.margin,  # type: ignore[arg-type]
            trade_gateway=self.trade,  # type: ignore[arg-type]
        )
        self.signal = _SignalInput(
            signal_id=_SIGNAL,
            symbol="XAUUSD",
            side="BUY",
            entry_price=Decimal("4000"),
            stop_loss=Decimal("3990"),
            take_profits=(Decimal("4010"), Decimal("4020"), Decimal("4030")),
            signal_requests_double_lot=False,
        )
        self.order_ids: list[str] = []

    def _load_inputs(self, owner_user_id: UUID, signal_id: UUID):  # type: ignore[override]
        _require(owner_user_id == _OWNER and signal_id == _SIGNAL, "input_identity")
        return self.signal, _AccountInput(UUID(int=100), "probe-metaapi-account", b"cipher")

    def _decrypt_token(self, account: _AccountInput) -> str:  # type: ignore[override]
        return "probe-token-with-terminal-access"

    def _create_planned_positions(self, *, signal, **_: object):  # type: ignore[override]
        return tuple(
            _PlannedPosition(
                local_position_id=UUID(int=index),
                tp_index=index,
                take_profit=tp,
                client_id=f"SS_{index:012d}_{index}",
            )
            for index, tp in enumerate(signal.take_profits, start=1)
        )

    def _record_order_id(self, local_position_id: UUID, order_id: str) -> None:
        self.order_ids.append(order_id)

    def _map_broker_positions(self, *, signal, sizing, planned, order_ids, **_):  # type: ignore[override]
        return tuple(
            Day26MappedPosition(
                local_position_id=item.local_position_id,
                tp_index=item.tp_index,
                take_profit=item.take_profit,
                volume=sizing.volume,
                client_id=item.client_id,
                broker_order_id=order_ids[item.client_id],
                broker_position_id=f"probe-position-{item.tp_index}",
                broker_open_price=signal.entry_price,
            )
            for item in planned
        )

    def _audit(self, **_: object) -> None:  # type: ignore[override]
        return None


class _AtomicHarness(AtomicDay26Mt5ExecutionService):
    def __init__(self, *, fail_on: int, close_fails: bool = False) -> None:
        self.read = _ReadGateway()
        self.margin = _MarginGateway()
        self.trade = _TradeGateway(fail_on=fail_on, close_fails=close_fails)
        super().__init__(
            session_factory=None,  # type: ignore[arg-type]
            cipher=None,  # type: ignore[arg-type]
            read_gateway=self.read,  # type: ignore[arg-type]
            margin_gateway=self.margin,  # type: ignore[arg-type]
            trade_gateway=self.trade,  # type: ignore[arg-type]
        )
        self.signal = _SignalInput(
            signal_id=_SIGNAL,
            symbol="XAUUSD",
            side="BUY",
            entry_price=Decimal("4000"),
            stop_loss=Decimal("3990"),
            take_profits=(Decimal("4010"), Decimal("4020"), Decimal("4030")),
            signal_requests_double_lot=False,
        )
        self.planned = tuple(
            _PlannedPosition(
                local_position_id=UUID(int=index),
                tp_index=index,
                take_profit=tp,
                client_id=f"SS_{index:012d}_{index}",
            )
            for index, tp in enumerate(self.signal.take_profits, start=1)
        )
        self.order_ids: dict[UUID, str] = {}
        self.rollback_audit: dict[str, object] | None = None

    def _load_inputs(self, owner_user_id: UUID, signal_id: UUID):  # type: ignore[override]
        return self.signal, _AccountInput(UUID(int=100), "probe-metaapi-account", b"cipher")

    def _decrypt_token(self, account: _AccountInput) -> str:  # type: ignore[override]
        return "probe-token-with-terminal-access"

    def _create_planned_positions(self, **_: object):  # type: ignore[override]
        return self.planned

    def _record_order_id(self, local_position_id: UUID, order_id: str) -> None:
        self.order_ids[local_position_id] = order_id
        item = next(row for row in self.planned if row.local_position_id == local_position_id)
        self.read.positions.append(
            {
                "id": f"probe-position-{item.tp_index}",
                "clientId": item.client_id,
                "symbol": "XAUUSD",
                "type": "POSITION_TYPE_BUY",
                "volume": 0.01,
                "stopLoss": 3990.0,
                "takeProfit": float(item.take_profit),
                "openPrice": 4000.0,
            }
        )

    def _map_broker_positions(self, **_: object):  # type: ignore[override]
        raise RuntimeError("probe should fail before mapping")

    def _audit(self, **_: object) -> None:  # type: ignore[override]
        return None

    def _rollback_rows(self, owner_user_id: UUID, signal_id: UUID) -> list[dict]:
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
        return "probe-metaapi-account", "probe-token-with-terminal-access"

    def _mark_unsubmitted_failed(self, local_rows: list[dict], original_code: str) -> None:
        return None

    def _mark_rollback_unresolved(self, local_rows: list[dict], original_code: str) -> None:
        return None

    def _persist_rollback_state(self, **_: object) -> None:
        return None

    def _audit_rollback(self, **kwargs: object) -> None:
        self.rollback_audit = kwargs


async def run_day26_code_acceptance_probe() -> None:
    """Exercise the actual Day 26 success and failure-atomic orchestration."""
    original_day23 = day26_module.Day23Mt5ReadService
    original_atomic_day23 = atomic_module.Day23Mt5ReadService
    day26_module.Day23Mt5ReadService = _FakeDay23  # type: ignore[assignment]
    atomic_module.Day23Mt5ReadService = _FakeDay23  # type: ignore[assignment]
    try:
        success = _SuccessHarness()
        result = await success.execute_owner_demo_signal(
            owner_user_id=_OWNER,
            signal_id=_SIGNAL,
            risk_percent="1",
            double_lot_approved=False,
        )
        _require(len(result.positions) == 3, "success_position_count")
        _require(len(success.trade.place_calls) == 3, "success_order_count")
        _require(len(success.margin.calls) == 1, "margin_must_be_checked_once")
        _require(
            [row["take_profit"] for row in success.trade.place_calls]
            == [4010.0, 4020.0, 4030.0],
            "provider_tp_mapping",
        )
        _require(
            {row["stop_loss"] for row in success.trade.place_calls} == {3990.0},
            "shared_stop_loss",
        )
        _require(
            len({row["client_id"] for row in success.trade.place_calls}) == 3,
            "unique_client_ids",
        )

        second = _AtomicHarness(fail_on=2)
        try:
            await second.execute_owner_demo_signal(
                owner_user_id=_OWNER,
                signal_id=_SIGNAL,
                risk_percent="1",
                double_lot_approved=False,
            )
        except Day26ExecutionError as exc:
            _require(exc.code == "metaapi_trade_rejected", "tp2_original_error")
        else:
            raise RuntimeError("day26_code_probe_failed:tp2_failure_not_raised")
        _require(second.trade.close_calls == ["probe-position-1"], "tp2_rollback")
        _require(
            second.rollback_audit is not None
            and second.rollback_audit.get("unresolved_count") == 0,
            "tp2_rollback_audit",
        )

        third = _AtomicHarness(fail_on=3)
        try:
            await third.execute_owner_demo_signal(
                owner_user_id=_OWNER,
                signal_id=_SIGNAL,
                risk_percent="1",
                double_lot_approved=False,
            )
        except Day26ExecutionError as exc:
            _require(exc.code == "metaapi_trade_rejected", "tp3_original_error")
        else:
            raise RuntimeError("day26_code_probe_failed:tp3_failure_not_raised")
        _require(
            third.trade.close_calls == ["probe-position-2", "probe-position-1"],
            "tp3_rollback",
        )

        rollback_failure = _AtomicHarness(fail_on=2, close_fails=True)
        try:
            await rollback_failure.execute_owner_demo_signal(
                owner_user_id=_OWNER,
                signal_id=_SIGNAL,
                risk_percent="1",
                double_lot_approved=False,
            )
        except Day26ExecutionError as exc:
            _require(
                exc.code == "day26_partial_execution_rollback_failed",
                "rollback_failure_escalation",
            )
        else:
            raise RuntimeError("day26_code_probe_failed:rollback_failure_not_raised")

        logger.info(
            "Day 26 code acceptance PASSED success_orders=3 rollback_tp2=1 rollback_tp3=2"
        )
    finally:
        day26_module.Day23Mt5ReadService = original_day23
        atomic_module.Day23Mt5ReadService = original_atomic_day23
