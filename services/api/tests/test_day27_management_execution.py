import asyncio
from dataclasses import replace
from decimal import Decimal
from uuid import UUID

import pytest

from app.metaapi_gateway import MetaApiGatewayError
from app.mt5_management_day27 import (
    Day27ManagementError,
    Day27Mt5ManagementService,
    _Account,
    _LocalPosition,
)

OWNER = UUID("ea604df2-f8ee-47d1-bc51-f0078dbf160d")
SIGNAL = UUID("198d5d81-f8c5-45d5-a368-cdf5841e744d")
EVENT = UUID("10000000-0000-4000-8000-000000000027")


class _Cipher:
    def decrypt(self, value: bytes) -> str:
        assert value == b"cipher"
        return "test-token"


class _Read:
    def __init__(self) -> None:
        self.positions = {
            "p1": {"id": "p1", "openPrice": 4370.82, "stopLoss": 4360.0, "takeProfit": 4380.0},
            "p2": {"id": "p2", "openPrice": 4370.81, "stopLoss": 4360.0, "takeProfit": 4390.0},
            "p3": {"id": "p3", "openPrice": 4370.81, "stopLoss": 4360.0, "takeProfit": 4400.0},
        }
        self.orders = {"o1", "o2", "manual-order"}

    async def resolve_account_region(self, **_: object) -> str:
        return "london"

    async def read_positions(self, **_: object):
        return list(self.positions.values())

    async def read_orders(self, **_: object):
        return [{"id": value} for value in self.orders]


class _Trade:
    def __init__(self, read: _Read) -> None:
        self.read = read
        self.closed: list[str] = []
        self.modified: list[tuple[str, float | None, float | None]] = []
        self.cancelled: list[str] = []
        self.fail_modify = False

    async def close_position(self, *, position_id: str, **_: object) -> None:
        self.closed.append(position_id)
        self.read.positions.pop(position_id, None)

    async def modify_position(
        self,
        *,
        position_id: str,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        **_: object,
    ) -> None:
        if self.fail_modify:
            raise MetaApiGatewayError("metaapi_trade_rejected")
        self.modified.append((position_id, stop_loss, take_profit))
        position = self.read.positions[position_id]
        if stop_loss is not None:
            position["stopLoss"] = stop_loss
        if take_profit is not None:
            position["takeProfit"] = take_profit

    async def cancel_order(self, *, order_id: str, **_: object) -> None:
        self.cancelled.append(order_id)
        self.read.orders.discard(order_id)


class _Harness(Day27Mt5ManagementService):
    def __init__(self, actions) -> None:
        self.read = _Read()
        self.trade = _Trade(self.read)
        super().__init__(
            session_factory=None,  # type: ignore[arg-type]
            cipher=_Cipher(),  # type: ignore[arg-type]
            read_gateway=self.read,  # type: ignore[arg-type]
            trade_gateway=self.trade,  # type: ignore[arg-type]
        )
        self.actions = tuple(actions)
        self.positions = [
            _LocalPosition(UUID(int=1), 1, "p1", "o1", "open", Decimal("4360"), Decimal("4380")),
            _LocalPosition(UUID(int=2), 2, "p2", "o2", "open", Decimal("4360"), Decimal("4390")),
            _LocalPosition(UUID(int=3), 3, "p3", "filled-market-order", "open", Decimal("4360"), Decimal("4400")),
        ]
        self.failures: list[str] = []

    def _existing_success(self, owner_user_id, lifecycle_event_id):
        return None

    def _load_event(self, event_id):
        assert event_id == EVENT
        return {
            "signal_id": SIGNAL,
            "aggregate_result": {"revised_instruction": {"management_actions": list(self.actions)}},
        }

    def _load_account(self, owner_user_id):
        assert owner_user_id == OWNER
        return _Account(UUID(int=99), "account", b"cipher")

    def _load_positions(self, signal_id, user_id):
        assert signal_id == SIGNAL and user_id == OWNER
        return tuple(self.positions)

    def _reconcile_missing_positions(self, *, broker_position_ids, **_):
        count = 0
        updated = []
        for item in self.positions:
            if item.status == "open" and item.broker_position_id not in broker_position_ids:
                updated.append(replace(item, status="closed"))
                count += 1
            else:
                updated.append(item)
        self.positions = updated
        return count

    def _replace_local(self, position_id: UUID, **changes) -> None:
        self.positions = [
            replace(item, **changes) if item.id == position_id else item for item in self.positions
        ]

    def _mark_provider_closed(self, position_id):
        self._replace_local(position_id, status="closed")

    def _update_local_stop(self, position_id, value):
        self._replace_local(position_id, stop_loss=value)

    def _update_local_tp(self, position_id, value):
        self._replace_local(position_id, take_profit=value)

    def _audit_success(self, result, actions):
        return None

    def _audit_failure(self, **kwargs):
        self.failures.append(str(kwargs["error_code"]))


async def _run(service: _Harness):
    return await service.execute_owner_demo_event(owner_user_id=OWNER, lifecycle_event_id=EVENT)


def test_manually_missing_position_is_reconciled_and_never_modified_or_reopened() -> None:
    service = _Harness([{"type": "move_to_break_even", "target": "all", "value": None}])
    service.read.positions.pop("p1")  # simulate a user/manual MT5 close before follow-up

    result = asyncio.run(_run(service))

    assert result.external_positions_reconciled == 1
    assert result.positions_modified == 2
    assert [item[0] for item in service.trade.modified] == ["p2", "p3"]
    assert service.trade.closed == []
    assert service.positions[0].status == "closed"
    assert "p1" not in service.read.positions


def test_close_tp1_then_break_even_only_changes_survivors() -> None:
    service = _Harness(
        [
            {"type": "close", "target": "TP1", "value": None},
            {"type": "move_to_break_even", "target": "all", "value": None},
        ]
    )
    result = asyncio.run(_run(service))

    assert result.positions_closed == 1
    assert result.positions_modified == 2
    assert service.trade.closed == ["p1"]
    assert service.trade.modified == [
        ("p2", 4370.81, None),
        ("p3", 4370.81, None),
    ]


def test_numeric_stop_applies_only_to_remaining_positions() -> None:
    service = _Harness([{"type": "edit_stop_loss", "target": "all", "value": "4365"}])
    service.read.positions.pop("p2")
    result = asyncio.run(_run(service))

    assert result.external_positions_reconciled == 1
    assert service.trade.modified == [("p1", 4365.0, None), ("p3", 4365.0, None)]


def test_take_profit_change_targets_one_position() -> None:
    service = _Harness([{"type": "edit_take_profit", "target": "TP2", "value": "4395"}])
    result = asyncio.run(_run(service))

    assert result.positions_modified == 1
    assert service.trade.modified == [("p2", None, 4395.0)]


def test_cancel_pending_touches_only_mapped_active_order_ids() -> None:
    service = _Harness([{"type": "cancel_pending", "target": "all", "value": None}])
    result = asyncio.run(_run(service))

    assert result.orders_cancelled == 2
    assert service.trade.cancelled == ["o1", "o2"]
    assert "manual-order" in service.read.orders


def test_partial_management_failure_never_reopens_closed_position() -> None:
    service = _Harness(
        [
            {"type": "close", "target": "TP1", "value": None},
            {"type": "edit_stop_loss", "target": "all", "value": "4365"},
        ]
    )
    service.trade.fail_modify = True

    with pytest.raises(Day27ManagementError, match="metaapi_trade_rejected"):
        asyncio.run(_run(service))

    assert service.trade.closed == ["p1"]
    assert "p1" not in service.read.positions
    assert service.positions[0].status == "closed"
    assert service.failures == ["metaapi_trade_rejected"]
