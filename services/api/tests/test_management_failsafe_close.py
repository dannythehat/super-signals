"""``force_close_all_positions`` is the unambiguous fail-safe used when a provider's
management instruction could not be resolved after repeated retries (see
``management_reliability_runtime``). It must close every broker-confirmed open position
for the signal and never touch anything else, regardless of what the original -- stuck --
instruction's target token was.
"""

import asyncio
from dataclasses import replace
from decimal import Decimal
from uuid import UUID

from app.mt5_management_day27 import Day27ManagementError, Day27Mt5ManagementService, _Account, _LocalPosition

OWNER = UUID("ea604df2-f8ee-47d1-bc51-f0078dbf160d")
SIGNAL = UUID("198d5d81-f8c5-45d5-a368-cdf5841e744d")


class _Read:
    def __init__(self, positions: dict) -> None:
        self.positions = positions

    async def resolve_account_region(self, **_: object) -> str:
        return "london"

    async def read_positions(self, **_: object):
        return list(self.positions.values())


class _Trade:
    def __init__(self, read: _Read) -> None:
        self.read = read
        self.closed: list[str] = []

    async def close_position(self, *, position_id: str, **_: object) -> None:
        self.closed.append(position_id)
        self.read.positions.pop(position_id, None)


class _Cipher:
    def decrypt(self, value: bytes) -> str:
        assert value == b"cipher"
        return "test-token"


class _Harness(Day27Mt5ManagementService):
    def __init__(self, *, broker_positions: dict, local_positions: list[_LocalPosition], account=None) -> None:
        self.read = _Read(broker_positions)
        self.trade = _Trade(self.read)
        super().__init__(
            session_factory=None,  # type: ignore[arg-type]
            cipher=_Cipher(),  # type: ignore[arg-type]
            read_gateway=self.read,  # type: ignore[arg-type]
            trade_gateway=self.trade,  # type: ignore[arg-type]
        )
        self.positions = list(local_positions)
        self.account = account
        self.reconciled = 0
        self.marked_closed: list[UUID] = []

    def _load_account(self, owner_user_id):
        assert owner_user_id == OWNER
        return self.account

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
        self.reconciled += count
        return count

    def _mark_failsafe_closed(self, position_id: UUID) -> None:
        self.marked_closed.append(position_id)
        self.positions = [
            replace(item, status="closed") if item.id == position_id else item
            for item in self.positions
        ]


def _position(index: int, broker_id: str, status: str = "open") -> _LocalPosition:
    return _LocalPosition(
        UUID(int=index), index, broker_id, f"order-{index}", status, Decimal("4360"), Decimal("4380")
    )


def _run(service: _Harness) -> int:
    return asyncio.run(
        service.force_close_all_positions(owner_user_id=OWNER, signal_id=SIGNAL)
    )


def test_closes_every_broker_confirmed_open_position_regardless_of_count() -> None:
    service = _Harness(
        broker_positions={
            "p1": {"id": "p1"},
            "p2": {"id": "p2"},
            "p3": {"id": "p3"},
        },
        local_positions=[_position(1, "p1"), _position(2, "p2"), _position(3, "p3")],
        account=_Account(UUID(int=99), "account", b"cipher"),
    )

    closed = _run(service)

    assert closed == 3
    assert set(service.trade.closed) == {"p1", "p2", "p3"}
    assert set(service.marked_closed) == {UUID(int=1), UUID(int=2), UUID(int=3)}
    assert all(item.status == "closed" for item in service.positions)


def test_never_touches_a_position_already_closed_locally() -> None:
    service = _Harness(
        broker_positions={"p1": {"id": "p1"}},
        local_positions=[_position(1, "p1"), _position(2, "p2", status="closed")],
        account=_Account(UUID(int=99), "account", b"cipher"),
    )

    closed = _run(service)

    assert closed == 1
    assert service.trade.closed == ["p1"]


def test_reconciles_a_position_the_broker_no_longer_holds_without_closing_it_again() -> None:
    service = _Harness(
        broker_positions={"p1": {"id": "p1"}},
        local_positions=[_position(1, "p1"), _position(2, "p2")],
        account=_Account(UUID(int=99), "account", b"cipher"),
    )

    closed = _run(service)

    assert closed == 1
    assert service.reconciled == 1
    assert service.trade.closed == ["p1"]


def test_no_open_positions_is_a_clean_noop() -> None:
    service = _Harness(
        broker_positions={},
        local_positions=[],
        account=_Account(UUID(int=99), "account", b"cipher"),
    )

    closed = _run(service)

    assert closed == 0
    assert service.trade.closed == []


def test_missing_account_fails_closed_rather_than_guessing() -> None:
    service = _Harness(broker_positions={}, local_positions=[], account=None)

    try:
        _run(service)
        raise AssertionError("expected Day27ManagementError")
    except Day27ManagementError as exc:
        assert exc.code == "mt5_account_not_configured"
