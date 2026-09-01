from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from app.dashboard_day32 import (
    Day32Account,
    Day32Connection,
    Day32DashboardView,
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
