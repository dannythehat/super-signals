"""Day 38 multi-user distribution and isolation acceptance."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import app.mt5_execution_day26 as day26_module
from app.day28_full_execution import _StoredDecision
from app.day38_full_execution import Day38FullExecutionRouter
from app.metaapi_trade_gateway import MetaApiMarketOrderResult
from app.mt5_execution_day26 import (
    Day26ExecutionError,
    Day26MappedPosition,
    _AccountInput,
    _PlannedPosition,
    _SignalInput,
)
from app.mt5_execution_day26_atomic import Day26RollbackResult
from app.mt5_execution_day38 import Day38LiveUserExecutionService
from app.mt5_management_day27 import Day27ManagementError
from app.multi_user_distribution_day38 import (
    Day38DistributionResult,
    Day38DistributionTarget,
    Day38MultiUserDistributionService,
    Day38UserDistributionOutcome,
)
from app.multi_user_management_day38 import Day38MultiUserManagementService
from app.mt5_read_service_day23 import Day23AccountState, Day23LiveState, Day23PriceState

U1 = UUID("11111111-1111-4111-8111-111111111111")
U2 = UUID("22222222-2222-4222-8222-222222222222")
U3 = UUID("33333333-3333-4333-8333-333333333333")
OWNER = UUID("44444444-4444-4444-8444-444444444444")
SIGNAL = UUID("55555555-5555-4555-8555-555555555555")


def _state(user_id: UUID) -> Day23LiveState:
    values = {
        U1: (1000.0, 1000.0),
        U2: (2000.0, 2000.0),
        U3: (1000.0, 10.0),
    }
    balance, free_margin = values[user_id]
    now = datetime.now(UTC)
    return Day23LiveState(
        local_account_id=uuid4(),
        metaapi_account_id=f"acct-{user_id.hex[:4]}",
        login_masked="****1234",
        server="Vantage-Live",
        region="london",
        read_at=now,
        account=Day23AccountState(
            currency="USD", balance=balance, equity=balance, margin=0.0,
            free_margin=free_margin, margin_level=None, leverage=500.0,
            trade_allowed=True,
        ),
        price=Day23PriceState(
            symbol="XAUUSD", bid=3999.9, ask=4000.0, buy_price=4000.0,
            sell_price=3999.9, quote_time=now, quote_age_seconds=0.1,
            available=True, stale=False, execution_ready=True, block_reason=None,
            profit_tick_value=1.0, loss_tick_value=1.0,
        ),
        positions=(), execution_ready=True, execution_block_reason=None,
    )


class _FakeDay23:
    def __init__(self, **_: object) -> None: pass
    @staticmethod
    def executable_price(state: Day23LiveState, side: str) -> float:
        return float(state.price.ask if side.strip().upper() == "BUY" else state.price.bid)
    async def read_owner_live_state(self, user_id: UUID) -> Day23LiveState:
        return _state(user_id)


class _Read:
    async def read_symbol_specification(self, **_: object) -> dict[str, object]:
        return {"tickSize": 0.01, "minVolume": 0.01, "maxVolume": 100.0, "volumeStep": 0.01}
    async def read_positions(self, **_: object): return []


class _Margin:
    async def calculate_margin(self, **_: object) -> float: return 50.0


class _Trade:
    def __init__(self) -> None: self.calls: list[dict[str, object]] = []
    async def place_market_order(self, **kwargs: object) -> MetaApiMarketOrderResult:
        self.calls.append(dict(kwargs))
        return MetaApiMarketOrderResult(
            order_id=f"order-{len(self.calls)}", position_id=f"position-{len(self.calls)}",
            numeric_code=10009, string_code="TRADE_RETCODE_DONE",
        )
    async def close_position(self, **_: object) -> None: return None


class _ExecutionHarness(Day38LiveUserExecutionService):
    def __init__(self) -> None:
        self.read, self.margin, self.trade = _Read(), _Margin(), _Trade()
        super().__init__(
            session_factory=None, cipher=None, read_gateway=self.read,
            margin_gateway=self.margin, trade_gateway=self.trade,
        )  # type: ignore[arg-type]
        self.signal = _SignalInput(
            signal_id=SIGNAL, symbol="XAUUSD", side="BUY",
            entry_low=Decimal("4000"), entry_high=Decimal("4000"),
            stop_loss=Decimal("3999"),
            take_profits=(Decimal("4010"), Decimal("4020"), Decimal("4030")),
            has_open_runner=False, signal_requests_double_lot=True,
            source_revision_index=0, source_posted_at=datetime.now(UTC),
        )
    def _provider_zone(self, signal_id): return Decimal("4000"), Decimal("4000")
    def _load_live_preferences(self, user_id: UUID):
        return {U1: ("0.5", False), U2: ("2", True), U3: ("1", False)}[user_id]
    def _load_inputs(self, user_id: UUID, signal_id: UUID):
        return self.signal, _AccountInput(uuid4(), f"acct-{user_id.hex[:4]}", b"cipher")
    def _decrypt_token(self, account): return "test-token-with-terminal-access"
    def _assert_signal_still_current(self, owner_user_id, signal): return None
    def _create_planned_positions(self, *, owner_user_id, signal, sizing, execution_entry):
        return tuple(
            _PlannedPosition(UUID(int=index), index, tp, f"SS_{owner_user_id.hex[:8]}_{index}")
            for index, tp in enumerate(signal.take_profits, 1)
        )
    def _record_order_id(self, local_position_id, order_id): return None
    def _map_broker_positions(self, *, owner_user_id, signal, sizing, execution_entry, planned, order_ids, broker_positions):
        return tuple(
            Day26MappedPosition(
                item.local_position_id, item.tp_index, item.take_profit, sizing.volume,
                item.client_id, order_ids[item.client_id], f"broker-{owner_user_id.hex[:4]}-{item.tp_index}",
                execution_entry,
            ) for item in planned
        )
    def _audit(self, **kwargs): return None
    def _audit_blocked(self, **kwargs): return None
    def _record_execution_failure(self, **kwargs): return None
    async def _compensate_partial_execution(self, **kwargs):
        return Day26RollbackResult(False, 0, 0, 0, 0)


class _DistributionHarness(Day38MultiUserDistributionService):
    def __init__(self, execution):
        self._execution = execution
        self._session_factory = None
    def _targets(self):
        return (
            Day38DistributionTarget(U1, Decimal("0.5"), False),
            Day38DistributionTarget(U2, Decimal("2"), True),
            Day38DistributionTarget(U3, Decimal("1"), False),
        )
    def _audit_user(self, **kwargs): return None
    def _audit_summary(self, result): return None


def test_one_signal_sizes_each_user_independently_and_underfunded_user_skips(monkeypatch) -> None:
    monkeypatch.setattr(day26_module, "Day23Mt5ReadService", _FakeDay23)
    execution = _ExecutionHarness()
    distribution = _DistributionHarness(execution)

    result = asyncio.run(distribution.distribute(signal_id=SIGNAL))

    by_user = {item.user_id: item for item in result.outcomes}
    assert result.target_count == 3
    assert result.executed_count == 2
    assert result.skipped_count == 1
    assert by_user[U1].volume_per_position == (Decimal("0.05"),) * 3
    # U2 chose 2% and allows the provider's explicit double-lot instruction -> 4% effective.
    assert by_user[U2].volume_per_position == (Decimal("0.8"),) * 3
    assert by_user[U3].position_count == 0
    assert by_user[U3].error_code == "insufficient_funds"
    assert len(execution.trade.calls) == 6
    assert {call["account_id"] for call in execution.trade.calls} == {"acct-1111", "acct-2222"}


class _OwnerExecution:
    async def execute_owner_demo_signal(self, **kwargs):
        raise Day26ExecutionError("mt5_account_not_connected")


class _MemberDistribution:
    async def distribute(self, *, signal_id: UUID):
        return Day38DistributionResult(
            signal_id=signal_id, target_count=2, executed_count=1, skipped_count=1,
            outcomes=(
                Day38UserDistributionOutcome(U1, "executed", Decimal("0.5"), False, 3, (Decimal("0.05"),)*3),
                Day38UserDistributionOutcome(U3, "skipped", Decimal("1"), False, 0, (), "insufficient_funds"),
            ),
        )


class _NoManagement:
    async def distribute(self, **kwargs): return SimpleNamespace(any_management_succeeded=False)


class _RouterHarness(Day38FullExecutionRouter):
    def __init__(self):
        self._owner_user_id = OWNER
        self._risk_percent = Decimal("1")
        self._double_lot_approved = True
        self._execution = _OwnerExecution()
        self._management = SimpleNamespace()
        self._member_distribution = _MemberDistribution()
        self._member_management = _NoManagement()
        self._locks = {}
        self.audits = []
    def _resolve_signal_id(self, message_id, revision_index): return SIGNAL
    def _position_count(self, signal_id): return 0
    def _prior_new_trade_route(self, signal_id): return None
    def _audit_success(self, **kwargs): self.audits.append(("success", kwargs))
    def _audit_failure(self, **kwargs): self.audits.append(("failure", kwargs))
    def _audit_day38_route(self, **kwargs): self.audits.append(("route", kwargs))


def test_owner_demo_failure_does_not_block_eligible_member_execution() -> None:
    router = _RouterHarness()
    result = asyncio.run(router._dispatch_new_trade(
        _StoredDecision(uuid4(), "new_trade", "execute", "v1_complete_signal"), 0
    ))
    assert result.outcome == "executed"
    assert result.position_count == 3
    success_payload = next(item[1]["payload"] for item in router.audits if item[0] == "success")
    assert success_payload["owner_reference_executed"] is False
    assert success_payload["member_executed_count"] == 1
    assert success_payload["owner_failure_blocked_members"] is False


class _ManagementHarness(Day38MultiUserManagementService):
    def __init__(self):
        self._session_factory = None
        self._management = self
        self.calls = []
    def _targets(self, signal_id): return (U1, U2)
    async def execute_owner_demo_event(self, *, owner_user_id, lifecycle_event_id):
        self.calls.append(owner_user_id)
        if owner_user_id == U1:
            return SimpleNamespace(
                already_applied=False, broker_actions_sent=2, positions_closed=0,
                positions_modified=2, orders_cancelled=0,
            )
        raise Day27ManagementError("mt5_account_not_connected")
    def _audit_user(self, *args, **kwargs): return None
    def _audit_summary(self, result): return None


def test_one_member_management_failure_does_not_block_another_member() -> None:
    service = _ManagementHarness()
    result = asyncio.run(service.distribute(signal_id=SIGNAL, lifecycle_event_id=uuid4()))
    assert service.calls == [U1, U2]
    assert result.managed_count == 1
    assert result.skipped_count == 1
    assert result.any_management_succeeded is True
