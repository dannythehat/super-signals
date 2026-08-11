"""One-shot pure-code acceptance probe for Day 26 V1 execution.

The probe never contacts MetaAPI and never writes to PostgreSQL. It exercises the
real Day 24, Day 25 and Day 26 orchestration using deterministic fake broker readers
and gateways. Enable temporarily with SUPER_SIGNALS_DAY26_CODE_PROBE=1.
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


def _live_state(*, bid: float = 3999.8, ask: float = 4000.0) -> Day23LiveState:
    now = datetime.now(UTC)
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
    state = _live_state()

    def __init__(self, **_: object) -> None:
        pass

    @staticmethod
    def executable_price(state: Day23LiveState, side: str) -> float:
        normalized = side.strip().upper()
        if normalized == "BUY":
            return float(state.price.ask)
        if normalized == "SELL":
            return float(state.price.bid)
        raise Day26ExecutionError("trade_side_invalid")

    async def read_owner_live_state(self, owner_user_id: UUID) -> Day23LiveState:
        _require(owner_user_id == _OWNER, "owner_mismatch")
        return self.__class__.state


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
    def __init__(
        self,
        *,
        entry_low: Decimal = Decimal("4000"),
        entry_high: Decimal = Decimal("4000"),
        runner: bool = False,
    ) -> None:
        self.read = _ReadGateway()
        self.margin = _MarginGateway()
        self.trade = _TradeGateway()
        super().__init__(
            session_factory=None,  # type: ignore[arg-type]
            cipher=None,  # type: ignore[arg-type]
            read_gateway=self.read,  # type: ignore[arg-type]
            margin_gateway=self.margin,  # type: ignore[arg-type]
            trade_gateway=self.trade,  # type: ignore[arg-type]
            zone_wait_seconds=0,
        )
        self.signal = _SignalInput(
            signal_id=_SIGNAL,
            symbol="XAUUSD",
            side="BUY",
            entry_low=entry_low,
            entry_high=entry_high,
            stop_loss=entry_low - Decimal("10"),
            take_profits=(
                entry_high + Decimal("10"),
                entry_high + Decimal("20"),
                entry_high + Decimal("30"),
            ),
            has_open_runner=runner,
            signal_requests_double_lot=False,
            source_revision_index=0,
            source_posted_at=datetime.now(UTC),
        )
        self.order_ids: list[str] = []

    def _load_inputs(self, owner_user_id: UUID, signal_id: UUID):  # type: ignore[override]
        _require(owner_user_id == _OWNER and signal_id == _SIGNAL, "input_identity")
        return self.signal, _AccountInput(UUID(int=100), "probe-metaapi-account", b"cipher")

    def _decrypt_token(self, account: _AccountInput) -> str:  # type: ignore[override]
        return "probe-token-with-terminal-access"

    def _assert_signal_still_current(self, owner_user_id: UUID, signal: _SignalInput) -> None:  # type: ignore[override]
        _require(owner_user_id == _OWNER and signal.signal_id == _SIGNAL, "freeze_check")

    def _create_planned_positions(self, *, signal, **_: object):  # type: ignore[override]
        targets: list[Decimal | None] = list(signal.take_profits)
        if signal.has_open_runner:
            targets.append(None)
        return tuple(
            _PlannedPosition(
                local_position_id=UUID(int=index),
                tp_index=index,
                take_profit=tp,
                client_id=f"SS_{index:012d}_{index}",
            )
            for index, tp in enumerate(targets, start=1)
        )

    def _record_order_id(self, local_position_id: UUID, order_id: str) -> None:
        self.order_ids.append(order_id)

    def _map_broker_positions(
        self,
        *,
        sizing,
        execution_entry,
        planned,
        order_ids,
        **_,
    ):  # type: ignore[override]
        return tuple(
            Day26MappedPosition(
                local_position_id=item.local_position_id,
                tp_index=item.tp_index,
                take_profit=item.take_profit,
                volume=sizing.volume,
                client_id=item.client_id,
                broker_order_id=order_ids[item.client_id],
                broker_position_id=f"probe-position-{item.tp_index}",
                broker_open_price=execution_entry,
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
            entry_low=Decimal("4000"),
            entry_high=Decimal("4000"),
            stop_loss=Decimal("3990"),
            take_profits=(Decimal("4010"), Decimal("4020"), Decimal("4030")),
            has_open_runner=False,
            signal_requests_double_lot=False,
            source_revision_index=0,
            source_posted_at=datetime.now(UTC),
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

    def _assert_signal_still_current(self, owner_user_id: UUID, signal: _SignalInput) -> None:  # type: ignore[override]
        return None

    def _create_planned_positions(self, **_: object):  # type: ignore[override]
        return self.planned

    def _record_order_id(self, local_position_id: UUID, order_id: str) -> None:
        self.order_ids[local_position_id] = order_id
        item = next(row for row in self.planned if row.local_position_id == local_position_id)
        _require(item.take_profit is not None, "rollback_numeric_tp")
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
    """Exercise exact, zone, runner and partial-failure compensation."""
    original_day23 = day26_module.Day23Mt5ReadService
    original_atomic_day23 = atomic_module.Day23Mt5ReadService
    day26_module.Day23Mt5ReadService = _FakeDay23  # type: ignore[assignment]
    atomic_module.Day23Mt5ReadService = _FakeDay23  # type: ignore[assignment]
    try:
        _FakeDay23.state = _live_state(bid=3999.8, ask=4000.0)
        exact = _SuccessHarness()
        exact_result = await exact.execute_owner_demo_signal(
            owner_user_id=_OWNER,
            signal_id=_SIGNAL,
            risk_percent="1",
            double_lot_approved=False,
        )
        _require(len(exact_result.positions) == 3, "exact_position_count")
        _require(len(exact.trade.place_calls) == 3, "exact_order_count")
        _require(len(exact.margin.calls) == 1, "exact_margin_once")

        _FakeDay23.state = _live_state(bid=4391.8, ask=4392.0)
        zone = _SuccessHarness(
            entry_low=Decimal("4389"),
            entry_high=Decimal("4394"),
        )
        zone_result = await zone.execute_owner_demo_signal(
            owner_user_id=_OWNER,
            signal_id=_SIGNAL,
            risk_percent="1",
            double_lot_approved=False,
        )
        _require(zone_result.signal_entry_price == Decimal("4392.0"), "zone_live_ask")
        _require(len(zone.trade.place_calls) == 3, "zone_order_count")

        _FakeDay23.state = _live_state(bid=3999.8, ask=4000.0)
        runner = _SuccessHarness(runner=True)
        runner_result = await runner.execute_owner_demo_signal(
            owner_user_id=_OWNER,
            signal_id=_SIGNAL,
            risk_percent="1",
            double_lot_approved=False,
        )
        _require(len(runner_result.positions) == 4, "runner_position_count")
        _require(runner.trade.place_calls[-1]["take_profit"] is None, "runner_no_tp")
        _require(runner.margin.calls[0]["volume"] == 0.04, "runner_margin_full_set")

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
            "Day 26 V1 code acceptance PASSED exact=3 zone=3 runner=4 rollback_tp2=1 rollback_tp3=2"
        )
    finally:
        day26_module.Day23Mt5ReadService = original_day23
        atomic_module.Day23Mt5ReadService = original_atomic_day23
