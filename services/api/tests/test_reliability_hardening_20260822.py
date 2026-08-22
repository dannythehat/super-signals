from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.mt5_management_day27 import Day27ManagementError, Day27ManagementResult
from app.paper_critical_management import PaperCriticalManagementService
from app.performance_ledger_canonical import (
    CanonicalPerformanceLedgerService,
    _resolve_mapped_position,
    _unique_position_index,
)
from app.trading_management_canonical import CanonicalTradingManagementService


class _ScalarResult:
    def __init__(self, value) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _CutoffSession:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, *args, **kwargs):
        return _ScalarResult(datetime.now(UTC))


class _ManagementRetryHarness(CanonicalTradingManagementService):
    def __init__(self) -> None:
        self._session_factory = lambda: _CutoffSession()
        self.signal_id = uuid4()

    def _load_event(self, event_id):
        return {
            "signal_id": self.signal_id,
            "aggregate_result": {
                "revised_instruction": {
                    "management_actions": [
                        {"type": "move_to_break_even", "target": "all", "value": None}
                    ]
                }
            },
        }


@pytest.mark.asyncio
async def test_explicit_broker_rejection_retries_once_through_shared_management_engine(
    monkeypatch,
) -> None:
    service = _ManagementRetryHarness()
    owner = uuid4()
    event = uuid4()
    attempts: list[int] = []
    sleeps: list[float] = []

    async def fake_base_execute(self, *, owner_user_id, lifecycle_event_id):
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            raise Day27ManagementError("metaapi_trade_rejected")
        return Day27ManagementResult(
            lifecycle_event_id=lifecycle_event_id,
            signal_id=service.signal_id,
            user_id=owner_user_id,
            actions_requested=1,
            broker_actions_sent=1,
            positions_closed=0,
            positions_modified=1,
            orders_cancelled=0,
            external_positions_reconciled=0,
        )

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(
        PaperCriticalManagementService,
        "execute_owner_demo_event",
        fake_base_execute,
    )
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    result = await service.execute_owner_demo_event(
        owner_user_id=owner,
        lifecycle_event_id=event,
    )

    assert result.broker_actions_sent == 1
    assert attempts == [1, 2]
    assert sleeps == [1.0]


@pytest.mark.asyncio
async def test_ambiguous_transport_failure_is_never_automatically_retried(monkeypatch) -> None:
    service = _ManagementRetryHarness()
    attempts: list[int] = []
    sleeps: list[float] = []

    async def fake_base_execute(self, *, owner_user_id, lifecycle_event_id):
        attempts.append(len(attempts) + 1)
        raise Day27ManagementError("metaapi_timeout", retryable=True)

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(
        PaperCriticalManagementService,
        "execute_owner_demo_event",
        fake_base_execute,
    )
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    with pytest.raises(Day27ManagementError, match="metaapi_timeout"):
        await service.execute_owner_demo_event(
            owner_user_id=uuid4(),
            lifecycle_event_id=uuid4(),
        )

    assert attempts == [1]
    assert sleeps == []


def test_broker_deal_mapping_falls_back_to_unique_client_id() -> None:
    row = {
        "id": uuid4(),
        "broker_position_id": "position-1",
        "broker_order_id": "order-1",
        "broker_client_id": "SS_abcdef123456_E1T1",
    }
    rows = [row]
    result = _resolve_mapped_position(
        broker_position_id="unknown-position",
        broker_order_id=None,
        broker_client_id="SS_abcdef123456_E1T1",
        by_broker_position=_unique_position_index(rows, "broker_position_id"),
        by_broker_order=_unique_position_index(rows, "broker_order_id"),
        by_broker_client=_unique_position_index(rows, "broker_client_id"),
    )
    assert result is row


def test_broker_deal_mapping_refuses_conflicting_identifiers() -> None:
    first = {
        "id": uuid4(),
        "broker_position_id": "position-1",
        "broker_order_id": "order-1",
        "broker_client_id": "SS_first_E1T1",
    }
    second = {
        "id": uuid4(),
        "broker_position_id": "position-2",
        "broker_order_id": "order-2",
        "broker_client_id": "SS_second_E1T1",
    }
    rows = [first, second]
    result = _resolve_mapped_position(
        broker_position_id="position-1",
        broker_order_id="order-2",
        broker_client_id=None,
        by_broker_position=_unique_position_index(rows, "broker_position_id"),
        by_broker_order=_unique_position_index(rows, "broker_order_id"),
        by_broker_client=_unique_position_index(rows, "broker_client_id"),
    )
    assert result is None


def test_duplicate_local_broker_identifier_is_not_used_for_mapping() -> None:
    rows = [
        {"id": uuid4(), "broker_client_id": "SS_duplicate_E1T1"},
        {"id": uuid4(), "broker_client_id": "SS_duplicate_E1T1"},
    ]
    assert _unique_position_index(rows, "broker_client_id") == {}


def test_canonical_sync_repairs_only_relationship_metadata() -> None:
    import inspect

    repair_source = inspect.getsource(CanonicalPerformanceLedgerService._repair_missing_deal_links)
    sync_source = inspect.getsource(CanonicalPerformanceLedgerService.sync_user)
    assert "signal_id IS NULL" in repair_source
    assert "^SSX?_" in repair_source
    assert "profit=" not in repair_source
    assert "price=" not in repair_source
    assert "volume=" not in repair_source
    assert "raw_payload=" not in repair_source
    assert "_repair_missing_deal_links" in sync_source
