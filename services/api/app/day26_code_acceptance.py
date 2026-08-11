"""Pure-code acceptance probe for Day 26 V1.

The probe never contacts MetaAPI and never writes PostgreSQL. It runs the real
Day24/Day25/Day26 orchestration with deterministic fake broker gateways.
Enable temporarily with SUPER_SIGNALS_DAY26_CODE_PROBE=1.
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
OWNER = UUID("ea604df2-f8ee-47d1-bc51-f0078dbf160d")
SIGNAL = UUID("3e7ec830-9a9a-42df-b908-b331863ff6a4")


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise RuntimeError(f"day26_code_probe_failed:{code}")


def _state(*, bid: float, ask: float) -> Day23LiveState:
    now = datetime.now(UTC)
    return Day23LiveState(
        local_account_id=UUID(int=99),
        metaapi_account_id="probe-account",
        login_masked="****0000",
        server="VantageMarkets-Demo",
        region="london",
        read_at=now,
        account=Day23AccountState(
            currency="USD",
            balance=10000.0,
            equity=10000.0,
            margin=0.0,
            free_margin=10000.0,
            margin_level=None,
            leverage=500.0,
            trade_allowed=True,
        ),
        price=Day23PriceState(
            symbol="XAUUSD",
            bid=bid,
            ask=ask,
            buy_price=ask,
            sell_price=bid,
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
    state = _state(bid=3999.8, ask=4000.0)

    def __init__(self, **_: object) -> None:
        pass

    @staticmethod
    def executable_price(state: Day23LiveState, side: str) -> float:
        if side.strip().upper() == "BUY":
            return float(state.price.ask)
        if side.strip().upper() == "SELL":
            return float(state.price.bid)
        raise Day26ExecutionError("trade_side_invalid")

    async def read_owner_live_state(self, owner_user_id: UUID) -> Day23LiveState:
        _require(owner_user_id == OWNER, "owner")
        return self.__class__.state


class _Read:
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


class _Margin:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def calculate_margin(self, **kwargs: object) -> float:
        self.calls.append(kwargs)
        return 50.0


class _Trade:
    def __init__(self, *, fail_on: int | None = None, close_fails: bool = False) -> None:
        self.fail_on = fail_on
        self.close_fails = close_fails
        self.place_calls: list[dict[str, object]] = []
        self.close_calls: list[str] = []

    async def place_market_order(self, **kwargs: object) -> MetaApiMarketOrderResult:
        self.place_calls.append(kwargs)
        number = len(self.place_calls)
        if self.fail_on == number:
            raise MetaApiGatewayError("metaapi_trade_rejected")
        return MetaApiMarketOrderResult(
            order_id=f"probe-order-{number}",
            position_id=f"probe-position-{number}",
            numeric_code=10009,
            string_code="TRADE_RETCODE_DONE",
        )

    async def close_position(self, *, position_id: str, **_: object) -> None:
        self.close_calls.append(position_id)
        if self.close_fails:
            raise MetaApiGatewayError("metaapi_trade_rejected")


def _signal(*, low: str = "4000", high: str = "4000", runner: bool = False) -> _SignalInput:
    low_value = Decimal(low)
    high_value = Decimal(high)
    return _SignalInput(
        signal_id=SIGNAL,
        symbol="XAUUSD",
        side="BUY",
        entry_low=low_value,
        entry_high=high_value,
        stop_loss=low_value - Decimal("10"),
        take_profits=(
            high_value + Decimal("10"),
            high_value + Decimal("20"),
            high_value + Decimal("30"),
        ),
        has_open_runner=runner,
        signal_requests_double_lot=False,
        source_revision_index=0,
        source_posted_at=datetime.now(UTC),
    )


class _Success(Day26Mt5ExecutionService):
    def __init__(self, signal: _SignalInput) -> None:
        self.signal = signal
        self.read = _Read()
        self.margin = _Margin()
        self.trade = _Trade()
        super().__init__(
            session_factory=None,  # type: ignore[arg-type]
            cipher=None,  # type: ignore[arg-type]
            read_gateway=self.read,  # type: ignore[arg-type]
            margin_gateway=self.margin,  # type: ignore[arg-type]
            trade_gateway=self.trade,  # type: ignore[arg-type]
            zone_wait_seconds=0,
        )

    def _load_inputs(self, owner_user_id: UUID, signal_id: UUID):  # type: ignore[override]
        _require(owner_user_id == OWNER and signal_id == SIGNAL, "input")
        return self.signal, _AccountInput(UUID(int=100), "probe-account", b"cipher")

    def _decrypt_token(self, account: _AccountInput) -> str:  # type: ignore[override]
        return "probe-token-with-terminal-access"

    def _assert_signal_still_current(self, owner_user_id: UUID, signal: _SignalInput) -> None:  # type: ignore[override]
        _require(owner_user_id == OWNER and signal.signal_id == SIGNAL, "freeze")

    def _create_planned_positions(self, *, signal, **_: object):  # type: ignore[override]
        targets: list[Decimal | None] = list(signal.take_profits)
        if signal.has_open_runner:
            targets.append(None)
        return tuple(
            _PlannedPosition(UUID(int=i), i, tp, f"SS_{i:012d}_{i}")
            for i, tp in enumerate(targets, 1)
        )

    def _record_order_id(self, local_position_id: UUID, order_id: str) -> None:
        pass

    def _map_broker_positions(self, *, sizing, execution_entry, planned, order_ids, **_):  # type: ignore[override]
        return tuple(
            Day26MappedPosition(
                item.local_position_id,
                item.tp_index,
                item.take_profit,
                sizing.volume,
                item.client_id,
                order_ids[item.client_id],
                f"probe-position-{item.tp_index}",
                execution_entry,
            )
            for item in planned
        )

    def _audit(self, **_: object) -> None:  # type: ignore[override]
        pass


class _Atomic(AtomicDay26Mt5ExecutionService):
    def __init__(self, *, fail_on: int, close_fails: bool = False) -> None:
        self.signal = _signal()
        self.read = _Read()
        self.margin = _Margin()
        self.trade = _Trade(fail_on=fail_on, close_fails=close_fails)
        self.order_ids: dict[UUID, str] = {}
        self.rollback_audit: dict[str, object] | None = None
        self.planned = tuple(
            _PlannedPosition(UUID(int=i), i, tp, f"SS_{i:012d}_{i}")
            for i, tp in enumerate(self.signal.take_profits, 1)
        )
        super().__init__(
            session_factory=None,  # type: ignore[arg-type]
            cipher=None,  # type: ignore[arg-type]
            read_gateway=self.read,  # type: ignore[arg-type]
            margin_gateway=self.margin,  # type: ignore[arg-type]
            trade_gateway=self.trade,  # type: ignore[arg-type]
        )

    def _load_inputs(self, owner_user_id: UUID, signal_id: UUID):  # type: ignore[override]
        return self.signal, _AccountInput(UUID(int=100), "probe-account", b"cipher")

    def _decrypt_token(self, account: _AccountInput) -> str:  # type: ignore[override]
        return "probe-token-with-terminal-access"

    def _assert_signal_still_current(self, owner_user_id: UUID, signal: _SignalInput) -> None:  # type: ignore[override]
        pass

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
                "volume": 0.1,
                "stopLoss": 3990.0,
                "takeProfit": float(item.take_profit),
                "openPrice": 4000.0,
            }
        )

    def _map_broker_positions(self, **_: object):  # type: ignore[override]
        raise RuntimeError("probe_should_fail_before_mapping")

    def _audit(self, **_: object) -> None:  # type: ignore[override]
        pass

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
        return "probe-account", "probe-token-with-terminal-access"

    def _mark_unsubmitted_failed(self, local_rows: list[dict], original_code: str) -> None:
        pass

    def _mark_rollback_unresolved(self, local_rows: list[dict], original_code: str) -> None:
        pass

    def _persist_rollback_state(self, **_: object) -> None:
        pass

    def _audit_rollback(self, **kwargs: object) -> None:
        self.rollback_audit = kwargs


async def run_day26_code_acceptance_probe() -> None:
    original_day23 = day26_module.Day23Mt5ReadService
    original_atomic_day23 = atomic_module.Day23Mt5ReadService
    day26_module.Day23Mt5ReadService = _FakeDay23  # type: ignore[assignment]
    atomic_module.Day23Mt5ReadService = _FakeDay23  # type: ignore[assignment]
    try:
        _FakeDay23.state = _state(bid=3999.8, ask=4000.0)
        exact = _Success(_signal())
        exact_result = await exact.execute_owner_demo_signal(
            owner_user_id=OWNER,
            signal_id=SIGNAL,
            risk_percent="1",
            double_lot_approved=False,
        )
        _require(len(exact_result.positions) == 3, "exact_positions")
        _require(len(exact.trade.place_calls) == 3, "exact_orders")
        _require(len(exact.margin.calls) == 1, "exact_margin")

        _FakeDay23.state = _state(bid=4391.8, ask=4392.0)
        zone = _Success(_signal(low="4389", high="4394"))
        zone_result = await zone.execute_owner_demo_signal(
            owner_user_id=OWNER,
            signal_id=SIGNAL,
            risk_percent="1",
            double_lot_approved=False,
        )
        _require(zone_result.signal_entry_price == Decimal("4392.0"), "zone_ask")
        _require(len(zone_result.positions) == 3, "zone_positions")

        _FakeDay23.state = _state(bid=3999.8, ask=4000.0)
        runner = _Success(_signal(runner=True))
        runner_result = await runner.execute_owner_demo_signal(
            owner_user_id=OWNER,
            signal_id=SIGNAL,
            risk_percent="1",
            double_lot_approved=False,
        )
        _require(len(runner_result.positions) == 4, "runner_positions")
        _require(runner.trade.place_calls[-1]["take_profit"] is None, "runner_no_tp")
        _require(len(runner.margin.calls) == 1, "runner_margin")

        second = _Atomic(fail_on=2)
        try:
            await second.execute_owner_demo_signal(
                owner_user_id=OWNER,
                signal_id=SIGNAL,
                risk_percent="1",
                double_lot_approved=False,
            )
        except Day26ExecutionError as exc:
            _require(exc.code == "metaapi_trade_rejected", "rollback2_original")
        else:
            raise RuntimeError("day26_code_probe_failed:rollback2_missing")
        _require(second.trade.close_calls == ["probe-position-1"], "rollback2_close")

        third = _Atomic(fail_on=3)
        try:
            await third.execute_owner_demo_signal(
                owner_user_id=OWNER,
                signal_id=SIGNAL,
                risk_percent="1",
                double_lot_approved=False,
            )
        except Day26ExecutionError as exc:
            _require(exc.code == "metaapi_trade_rejected", "rollback3_original")
        else:
            raise RuntimeError("day26_code_probe_failed:rollback3_missing")
        _require(
            third.trade.close_calls == ["probe-position-2", "probe-position-1"],
            "rollback3_close",
        )

        rollback_failure = _Atomic(fail_on=2, close_fails=True)
        try:
            await rollback_failure.execute_owner_demo_signal(
                owner_user_id=OWNER,
                signal_id=SIGNAL,
                risk_percent="1",
                double_lot_approved=False,
            )
        except Day26ExecutionError as exc:
            _require(
                exc.code == "day26_partial_execution_rollback_failed",
                "rollback_failure_code",
            )
        else:
            raise RuntimeError("day26_code_probe_failed:rollback_failure_missing")

        logger.info(
            "Day 26 V1 code acceptance PASSED exact=3 zone=3 runner=4 rollback_tp2=1 rollback_tp3=2"
        )
    finally:
        day26_module.Day23Mt5ReadService = original_day23
        atomic_module.Day23Mt5ReadService = original_atomic_day23
