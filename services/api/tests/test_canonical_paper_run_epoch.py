from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from app.dashboard_day32 import (
    Day32Account,
    Day32Connection,
    Day32DashboardService,
    Day32DashboardView,
    Day32OpenPosition,
    Day32Trading,
    Day32WinLoss,
)
from app.dashboard_runtime import (
    CanonicalDashboardRuntimeService,
    CanonicalTodayTradingSummaryService,
)
from app.dashboard_today_summary import TodayTradingSummaryService
from app.paper_run_epoch import (
    PAPER_RUN_BASELINE_BALANCE,
    PAPER_RUN_STARTED_AT,
    active_paper_epoch,
)
from app.performance_ledger_day33 import Day33PerformanceWindow
from app.performance_runtime import CanonicalPerformanceRuntimeService

OWNER = uuid4()
OTHER = uuid4()
ZERO = Decimal("0")
ROOT = Path(__file__).resolve().parents[3]


def _zero_window(key: str, label: str) -> Day33PerformanceWindow:
    return Day33PerformanceWindow(
        key=key,
        label=label,
        cash_pnl=ZERO,
        return_percent=None,
        model_500_pnl=ZERO,
        model_500_return_percent=ZERO,
        closed_trades=0,
        wins=0,
        losses=0,
        breakeven=0,
        open_trades=0,
        win_rate_percent=None,
        net_pips=None,
        mixed_instrument_pips=False,
    )


def test_paper_run_origin_is_fixed_to_accepted_clean_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPER_SIGNALS_DAY28_OWNER_ID", str(OWNER))
    monkeypatch.setenv("SUPER_SIGNALS_PAPER_RESET_AT", "2099-01-01T00:00:00Z")
    monkeypatch.setenv("SUPER_SIGNALS_PAPER_BASELINE_BALANCE", "999999")

    epoch = active_paper_epoch(OWNER)
    assert epoch is not None
    assert epoch.started_at == datetime(2026, 8, 30, 21, 0, tzinfo=UTC)
    assert epoch.started_at == PAPER_RUN_STARTED_AT
    assert epoch.baseline_balance == Decimal("1517.23")
    assert epoch.baseline_balance == PAPER_RUN_BASELINE_BALANCE
    assert active_paper_epoch(OTHER) is None


class _PerformanceHarness(CanonicalPerformanceRuntimeService):
    def __init__(self) -> None:
        self.calls: list[tuple[str, datetime | None, datetime]] = []

    def _signal_window(self, user_id, key, label, since, now):  # noqa: ANN001, ANN201
        self.calls.append((key, since, now))
        return Day33PerformanceWindow(
            key=key,
            label=label,
            cash_pnl=Decimal("31"),
            return_percent=Decimal("3.10"),
            model_500_pnl=ZERO,
            model_500_return_percent=ZERO,
            closed_trades=1,
            wins=1,
            losses=0,
            breakeven=0,
            open_trades=0,
            win_rate_percent=Decimal("100"),
            net_pips=None,
            mixed_instrument_pips=False,
        )


def test_origin_day_all_windows_accumulate_from_same_permanent_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUPER_SIGNALS_DAY28_OWNER_ID", str(OWNER))
    service = _PerformanceHarness()
    now = datetime(2026, 8, 31, 6, 0, tzinfo=UTC)

    windows = service.read_windows(OWNER, now=now)

    assert [item.key for item in windows] == ["today", "7d", "30d", "month", "all"]
    assert service.calls == [
        ("today", PAPER_RUN_STARTED_AT, now),
        ("7d", PAPER_RUN_STARTED_AT, now),
        ("30d", PAPER_RUN_STARTED_AT, now),
        ("month", PAPER_RUN_STARTED_AT, now),
        ("all", PAPER_RUN_STARTED_AT, now),
    ]
    assert all(item.cash_pnl == Decimal("31") for item in windows)
    assert all(item.closed_trades == 1 for item in windows)
    assert all(item.wins == 1 and item.losses == 0 and item.breakeven == 0 for item in windows)


def test_future_windows_can_never_reach_before_paper_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPER_SIGNALS_DAY28_OWNER_ID", str(OWNER))
    service = _PerformanceHarness()
    now = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)

    service.read_windows(OWNER, now=now)

    assert service.calls
    for _key, since, _point in service.calls:
        assert since is None or since >= PAPER_RUN_STARTED_AT


def _base_dashboard_view(open_profit: float) -> Day32DashboardView:
    position = Day32OpenPosition(
        position_id=uuid4(),
        broker_position_id="broker-position",
        signal_id=uuid4(),
        tp_index=1,
        symbol="XAUUSD",
        side="BUY",
        volume=0.01,
        planned_risk_percent=Decimal("1"),
        entry_price=4400.0,
        current_price=4401.0,
        stop_loss=4390.0,
        take_profit=4410.0,
        profit=open_profit,
        opened_at=PAPER_RUN_STARTED_AT + timedelta(minutes=5),
    )
    return Day32DashboardView(
        connection=Day32Connection(
            configured=True,
            status="connected",
            account_environment="demo",
            login_masked="****1913",
            server="Vantage",
            error_code=None,
            read_at=PAPER_RUN_STARTED_AT,
        ),
        account=Day32Account(
            currency="USD",
            balance=812.0,
            equity=810.0,
            margin=100.0,
            free_margin=710.0,
            trade_allowed=True,
        ),
        trading=Day32Trading(
            available=True,
            status="active",
            risk_percent=Decimal("1"),
            allow_double_lot=True,
            effective_double_lot_risk_percent=Decimal("2"),
        ),
        open_profit=open_profit,
        open_positions=(position,),
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
async def test_dashboard_owner_demo_shows_metaapi_account_values_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUPER_SIGNALS_DAY28_OWNER_ID", str(OWNER))
    base = _base_dashboard_view(open_profit=-2.5)

    async def fake_base_read(_self, user_id):
        assert user_id == OWNER
        return base

    monkeypatch.setattr(Day32DashboardService, "read", fake_base_read)
    service = object.__new__(CanonicalDashboardRuntimeService)

    result = await service.read(OWNER)

    assert result.account is not None
    assert result.account.balance == 812.0
    assert result.account.equity == 810.0
    assert result.account.free_margin == 710.0
    assert result.account.margin == 100.0
    assert result.open_profit == -2.5


def test_dashboard_runtime_preserves_metaapi_broker_truth() -> None:
    runtime = (ROOT / "services/api/app/dashboard_runtime.py").read_text(encoding="utf-8")
    assert "MetaAPI/MT5 truth" in runtime
    assert "displayed_balance" not in runtime
    assert "display_equity" not in runtime
    assert "display_free_margin" not in runtime


def test_today_session_starts_at_permanent_epoch_on_origin_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUPER_SIGNALS_DAY28_OWNER_ID", str(OWNER))
    day_start = datetime(2026, 8, 30, 21, 0, tzinfo=UTC)
    day_end = day_start + timedelta(days=1)

    monkeypatch.setattr(
        TodayTradingSummaryService,
        "_session_start",
        lambda _self, _session, _user_id, *, day_start, day_end: day_start,
    )
    service = object.__new__(CanonicalTodayTradingSummaryService)

    start = service._session_start(
        None,
        OWNER,
        day_start=day_start,
        day_end=day_end,
    )

    assert start == PAPER_RUN_STARTED_AT
