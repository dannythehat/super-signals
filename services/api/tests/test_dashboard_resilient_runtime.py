from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from app.dashboard_day32 import (
    Day32Account,
    Day32Connection,
    Day32DashboardView,
    Day32OpenPosition,
    Day32Trading,
    Day32WinLoss,
)
from app.dashboard_resilient_runtime import ResilientDashboardRuntimeService
from app.dashboard_runtime import CanonicalDashboardRuntimeService


def _view(*, account: Day32Account | None, status: str) -> Day32DashboardView:
    return Day32DashboardView(
        connection=Day32Connection(
            configured=True,
            status=status,
            account_environment="demo",
            login_masked="***1234",
            server="VantageMarkets-Demo",
            error_code=("metaapi_timeout" if status == "connection_error" else None),
            read_at=datetime.now(UTC),
        ),
        account=account,
        trading=Day32Trading(
            available=True,
            status="active",
            risk_percent=Decimal("1"),
            allow_double_lot=False,
            effective_double_lot_risk_percent=Decimal("1"),
        ),
        open_profit=None,
        open_positions=(),
        latest_signal=None,
        recent_completed=(),
        performance=(),
        win_loss=Day32WinLoss(
            wins=0,
            losses=0,
            breakeven=0,
            known_results=0,
            win_rate_percent=None,
        ),
        activity=(),
        reconciled_external_positions=0,
    )


def _position(
    *,
    broker_position_id: str,
    signal_id=None,  # noqa: ANN001
    profit: float | None = None,
) -> Day32OpenPosition:
    return Day32OpenPosition(
        position_id=uuid4(),
        broker_position_id=broker_position_id,
        signal_id=signal_id or uuid4(),
        tp_index=3,
        symbol="XAUUSD",
        side="SELL",
        volume=0.01,
        planned_risk_percent=Decimal("1"),
        entry_price=4403.92,
        current_price=(4390.0 if profit is not None else None),
        stop_loss=4420.0,
        take_profit=4360.0,
        profit=profit,
        opened_at=datetime.now(UTC),
    )


def test_dashboard_snapshot_can_never_close_a_missing_position() -> None:
    """A partial MetaAPI active-position list is not settlement authority."""
    service = object.__new__(ResilientDashboardRuntimeService)

    reconciled = service._reconcile_missing_open_positions(
        user_id=uuid4(),
        broker_position_ids=set(),
        now=datetime.now(UTC),
    )

    assert reconciled == 0


def test_partial_broker_snapshot_keeps_every_durable_open_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact regression: a just-opened TP leg omitted by one snapshot stays visible."""
    user_id = uuid4()
    signal_id = uuid4()
    live = _position(
        broker_position_id="1957256077",
        signal_id=signal_id,
        profit=16.93,
    )
    durable_live = replace(live, current_price=None, profit=None)
    omitted = _position(
        broker_position_id="1957256114",
        signal_id=signal_id,
        profit=None,
    )

    monkeypatch.setattr(
        CanonicalDashboardRuntimeService,
        "_mapped_open_positions",
        lambda self, requested_user_id, broker_positions: (live,),
    )
    monkeypatch.setattr(
        ResilientDashboardRuntimeService,
        "_durable_mapped_open_positions",
        lambda self, requested_user_id: (durable_live, omitted),
    )

    service = object.__new__(ResilientDashboardRuntimeService)
    result = service._mapped_open_positions(user_id, {})

    assert [item.broker_position_id for item in result] == [
        "1957256077",
        "1957256114",
    ]
    assert result[0] is live
    assert result[1] is omitted
    assert result[1].profit is None


def test_cached_dashboard_immediately_includes_new_durable_open_trade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one-minute broker refresh cooldown must not delay open-trade visibility."""
    user_id = uuid4()
    signal_id = uuid4()
    cached_position = _position(
        broker_position_id="existing",
        signal_id=signal_id,
        profit=5.0,
    )
    new_position = _position(
        broker_position_id="newly-opened",
        signal_id=signal_id,
        profit=None,
    )
    account = Day32Account(
        currency="USD",
        balance=1600.0,
        equity=1605.0,
        margin=5.0,
        free_margin=1600.0,
        trade_allowed=True,
    )
    cached = replace(
        _view(account=account, status="connected"),
        open_positions=(cached_position,),
        open_profit=5.0,
    )
    durable_existing = replace(cached_position, current_price=None, profit=None)

    monkeypatch.setattr(
        ResilientDashboardRuntimeService,
        "_durable_mapped_open_positions",
        lambda self, requested_user_id: (durable_existing, new_position),
    )

    service = object.__new__(ResilientDashboardRuntimeService)
    result = service._overlay_current_open_positions(user_id, cached)

    assert [item.broker_position_id for item in result.open_positions] == [
        "existing",
        "newly-opened",
    ]
    assert result.open_positions[0] is cached_position
    assert result.open_positions[1] is new_position
    assert result.open_profit is None


@pytest.mark.asyncio
async def test_connection_error_uses_last_confirmed_account(monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = uuid4()
    cached = Day32Account(
        currency="USD",
        balance=1576.65,
        equity=1576.65,
        margin=0.0,
        free_margin=1576.65,
        trade_allowed=True,
    )

    async def base_read(self, requested_user_id):  # noqa: ANN001
        assert requested_user_id == user_id
        return _view(account=None, status="connection_error")

    monkeypatch.setattr(CanonicalDashboardRuntimeService, "read", base_read)
    monkeypatch.setattr(
        ResilientDashboardRuntimeService,
        "_last_confirmed_account",
        lambda self, requested_user_id: cached if requested_user_id == user_id else None,
    )

    service = object.__new__(ResilientDashboardRuntimeService)
    result = await service.read(user_id)

    assert result.connection.status == "connection_error"
    assert result.account == cached


@pytest.mark.asyncio
async def test_successful_dashboard_read_updates_last_confirmed_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    live = Day32Account(
        currency="USD",
        balance=1600.0,
        equity=1605.0,
        margin=5.0,
        free_margin=1600.0,
        trade_allowed=True,
    )
    saved: list[Day32Account] = []

    async def base_read(self, requested_user_id):  # noqa: ANN001
        assert requested_user_id == user_id
        return _view(account=live, status="connected")

    monkeypatch.setattr(CanonicalDashboardRuntimeService, "read", base_read)
    monkeypatch.setattr(
        ResilientDashboardRuntimeService,
        "_persist_last_confirmed_account",
        lambda self, requested_user_id, account, read_at: saved.append(account),
    )

    service = object.__new__(ResilientDashboardRuntimeService)
    result = await service.read(user_id)

    assert result.account == live
    assert saved == [live]
