"""Dependency-free Day 28 acceptance checks for the Render production image."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

from app.day28_full_execution import Day28FullExecutionRouter, _StoredDecision
from app.day28_zone_guard import Day28ZoneGuardTradeGateway
from app.metaapi_gateway import MetaApiGatewayError
from app.mt5_execution_day26 import Day26ExecutionError


class _Execution:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.completed: dict[UUID, int] = {}
        self.failure_code: str | None = None

    async def execute_owner_demo_signal(self, **kwargs):
        self.calls.append(dict(kwargs))
        if self.failure_code:
            raise Day26ExecutionError(self.failure_code)
        signal_id = kwargs["signal_id"]
        self.completed[signal_id] = 3
        return SimpleNamespace(
            positions=(object(), object(), object()),
            double_lot_applied=True,
        )


class _Management:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.applied: set[UUID] = set()

    async def execute_owner_demo_event(self, **kwargs):
        self.calls.append(dict(kwargs))
        event_id = kwargs["lifecycle_event_id"]
        already = event_id in self.applied
        self.applied.add(event_id)
        return SimpleNamespace(
            actions_requested=1,
            broker_actions_sent=0 if already else 1,
            positions_closed=0 if already else 1,
            positions_modified=0,
            orders_cancelled=0,
            already_applied=already,
        )


class _Router(Day28FullExecutionRouter):
    def __init__(self, *, source_id: UUID, execution: _Execution, management: _Management) -> None:
        super().__init__(
            session_factory=lambda: None,
            owner_user_id=uuid4(),
            execution_service=execution,
            management_service=management,
            allowed_source_ids=(source_id,),
            risk_percent="1",
            double_lot_approved=True,
        )
        self.execution = execution
        self.management = management
        self.stored: _StoredDecision | None = None
        self.signal_id = uuid4()
        self.event_id = uuid4()
        self.load_calls = 0

    def _load_stored_decision(self, **kwargs):
        self.load_calls += 1
        return self.stored

    def _resolve_signal_id(self, message_id, revision_index):
        return self.signal_id

    def _resolve_lifecycle_event(self, message_id, revision_index):
        return self.event_id, self.signal_id

    def _has_position_records(self, signal_id):
        return signal_id in self.execution.completed

    def _position_count(self, signal_id):
        return self.execution.completed.get(signal_id, 0)

    def _audit_success(self, **kwargs):
        return None

    def _audit_failure(self, **kwargs):
        return None


def _decision(kind: str, action: str, reason: str = "acceptance") -> _StoredDecision:
    return _StoredDecision(message_id=uuid4(), decision=kind, action=action, reason=reason)


class _Read:
    def __init__(self, prices: list[dict[str, object]]) -> None:
        self.prices = list(prices)
        self.calls = 0

    async def read_symbol_price(self, **kwargs):
        self.calls += 1
        return self.prices.pop(0)


class _Trade:
    def __init__(self) -> None:
        self.market_calls: list[dict] = []

    async def place_market_order(self, **kwargs):
        self.market_calls.append(dict(kwargs))
        return object()

    async def close_position(self, **kwargs):
        return None

    async def modify_position(self, **kwargs):
        return None

    async def cancel_order(self, **kwargs):
        return None


_ORDER = {
    "token": "token",
    "account_id": "account",
    "region": "london",
    "side": "BUY",
    "symbol": "XAUUSD",
    "volume": 0.01,
    "stop_loss": 95.0,
    "take_profit": 105.0,
    "client_id": "SS_D28_ABC",
}


async def _route_checks() -> None:
    source = uuid4()
    execution = _Execution()
    management = _Management()
    router = _Router(source_id=source, execution=execution, management=management)

    router.stored = _decision("new_trade", "execute")
    first = await router.dispatch_stored_decision(source_id=source, telegram_message_id=1)
    replay = await router.dispatch_stored_decision(source_id=source, telegram_message_id=1)
    assert first.outcome == "executed" and first.position_count == 3
    assert replay.outcome == "already_applied"
    assert len(execution.calls) == 1
    assert execution.calls[0]["risk_percent"] == "1"
    assert execution.calls[0]["double_lot_approved"] is True

    router.stored = _decision("chatter", "ignore", "provider_chatter")
    chatter = await router.dispatch_stored_decision(source_id=source, telegram_message_id=2)
    assert chatter.outcome == "ignored"
    assert len(execution.calls) == 1 and not management.calls

    router.stored = _decision("trade_update", "apply_update")
    managed = await router.dispatch_stored_decision(source_id=source, telegram_message_id=3)
    managed_replay = await router.dispatch_stored_decision(source_id=source, telegram_message_id=3)
    assert managed.outcome == "managed" and managed.broker_actions_sent == 1
    assert managed_replay.outcome == "already_applied" and managed_replay.broker_actions_sent == 0

    failing_execution = _Execution()
    failing_execution.failure_code = "entry_price_unavailable"
    failing_router = _Router(source_id=source, execution=failing_execution, management=_Management())
    failing_router.stored = _decision("new_trade", "execute")
    failed = await failing_router.dispatch_stored_decision(source_id=source, telegram_message_id=4)
    assert failed.outcome == "blocked" and failed.error_code == "entry_price_unavailable"
    assert len(failing_execution.calls) == 1

    outside = await router.dispatch_stored_decision(source_id=uuid4(), telegram_message_id=5)
    assert outside.outcome == "ignored" and outside.reason == "source_not_in_day28_allowlist"


async def _zone_checks() -> None:
    read = _Read([
        {"ask": 100.00, "bid": 99.90},
        {"ask": 101.25, "bid": 101.15},
    ])
    base = _Trade()
    guarded = Day28ZoneGuardTradeGateway(base=base, read_gateway=read)
    token = guarded.set_zone(Decimal("99.50"), Decimal("100.50"))
    try:
        await guarded.place_market_order(**_ORDER)
        try:
            await guarded.place_market_order(**_ORDER)
        except MetaApiGatewayError as exc:
            assert exc.code == "zone_left_before_position_submission"
        else:
            raise AssertionError("zone guard did not stop the second leg")
    finally:
        guarded.reset_zone(token)
    assert read.calls == 2 and len(base.market_calls) == 1

    sell_read = _Read([{"ask": 100.80, "bid": 100.40}])
    sell_base = _Trade()
    sell_guard = Day28ZoneGuardTradeGateway(base=sell_base, read_gateway=sell_read)
    sell_token = sell_guard.set_zone(Decimal("100.00"), Decimal("100.50"))
    try:
        await sell_guard.place_market_order(**{**_ORDER, "side": "SELL"})
    finally:
        sell_guard.reset_zone(sell_token)
    assert sell_read.calls == 1 and len(sell_base.market_calls) == 1

    exact_read = _Read([])
    exact_base = _Trade()
    exact_guard = Day28ZoneGuardTradeGateway(base=exact_base, read_gateway=exact_read)
    await exact_guard.place_market_order(**_ORDER)
    assert exact_read.calls == 0 and len(exact_base.market_calls) == 1


async def run() -> None:
    await _route_checks()
    await _zone_checks()
    print(
        "Day 28 code acceptance PASSED: 1% + double toggle propagated; "
        "new-trade replay idempotent; chatter ignored; management replay idempotent; "
        "exact failure preserved/no retry; source allow-list enforced; per-leg zone guard passed"
    )


if __name__ == "__main__":
    asyncio.run(run())
