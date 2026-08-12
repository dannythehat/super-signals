"""Pure-code Day 27 acceptance probe used only during deliberate Render validation."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from uuid import UUID

from app.day27_management_policy import extract_day27_management_actions
from app.mt5_management_day27 import (
    Day27Mt5ManagementService,
    _Account,
    _LocalPosition,
)

_OWNER = UUID("ea604df2-f8ee-47d1-bc51-f0078dbf160d")
_SIGNAL = UUID("198d5d81-f8c5-45d5-a368-cdf5841e744d")
_EVENT = UUID("27000000-0000-4000-8000-000000000001")


class _Cipher:
    def decrypt(self, value: bytes) -> str:
        assert value == b"cipher"
        return "acceptance-token"


class _Read:
    def __init__(self) -> None:
        self.positions = {
            "p1": {"id": "p1", "openPrice": 4370.82, "stopLoss": 4360.0, "takeProfit": 4380.0},
            "p2": {"id": "p2", "openPrice": 4370.81, "stopLoss": 4360.0, "takeProfit": 4390.0},
            "p3": {"id": "p3", "openPrice": 4370.81, "stopLoss": 4360.0, "takeProfit": 4400.0},
        }
        self.orders = {"o2", "manual-order"}

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
        self.modified.append((position_id, stop_loss, take_profit))
        if stop_loss is not None:
            self.read.positions[position_id]["stopLoss"] = stop_loss
        if take_profit is not None:
            self.read.positions[position_id]["takeProfit"] = take_profit

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
            _LocalPosition(UUID(int=3), 3, "p3", "market-order", "open", Decimal("4360"), Decimal("4400")),
        ]

    def _existing_success(self, owner_user_id, lifecycle_event_id):
        return None

    def _load_event(self, event_id):
        assert event_id == _EVENT
        return {
            "signal_id": _SIGNAL,
            "aggregate_result": {"revised_instruction": {"management_actions": list(self.actions)}},
        }

    def _load_account(self, owner_user_id):
        assert owner_user_id == _OWNER
        return _Account(UUID(int=99), "account", b"cipher")

    def _load_positions(self, signal_id, user_id):
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

    def _replace(self, position_id, **changes):
        self.positions = [
            replace(item, **changes) if item.id == position_id else item
            for item in self.positions
        ]

    def _mark_provider_closed(self, position_id):
        self._replace(position_id, status="closed")

    def _update_local_stop(self, position_id, value):
        self._replace(position_id, stop_loss=value)

    def _update_local_tp(self, position_id, value):
        self._replace(position_id, take_profit=value)

    def _audit_success(self, result, actions):
        return None

    def _audit_failure(self, **kwargs):
        return None


async def run_day27_code_acceptance_probe() -> None:
    optional = extract_day27_management_actions(
        "Trade in +40 pips profit. Make the trade risk-free if you want"
    )
    assert optional.actions == ()
    assert optional.reason == "optional_management_instruction"

    explicit = extract_day27_management_actions(
        "TP1 HIT. Close TP1 now and move SL to 4385"
    )
    assert explicit.actions == (
        {"type": "close", "target": "TP1", "value": None},
        {"type": "edit_stop_loss", "target": "all", "value": "4385"},
    )

    service = _Harness(
        [
            {"type": "close", "target": "TP1", "value": None},
            {"type": "move_to_break_even", "target": "all", "value": None},
            {"type": "edit_take_profit", "target": "TP2", "value": "4395"},
            {"type": "cancel_pending", "target": "all", "value": None},
        ]
    )
    # Simulate a manual broker close of TP1 before the provider update arrives. The
    # service must reconcile it and must never submit a close or reopen action for p1.
    service.read.positions.pop("p1")
    result = await service.execute_owner_demo_event(
        owner_user_id=_OWNER,
        lifecycle_event_id=_EVENT,
    )
    assert result.external_positions_reconciled == 1
    assert service.trade.closed == []
    assert service.trade.modified == [
        ("p2", 4370.81, None),
        ("p3", 4370.81, None),
        ("p2", None, 4395.0),
    ]
    assert service.trade.cancelled == ["o2"]
    assert "manual-order" in service.read.orders
    assert service.positions[0].status == "closed"
    assert "p1" not in service.read.positions

    print(
        "Day 27 code acceptance PASSED: optional ignored, manual-close no-reopen, "
        "BE survivors=2, TP2 modified, mapped pending cancel only"
    )


__all__ = ["run_day27_code_acceptance_probe"]
