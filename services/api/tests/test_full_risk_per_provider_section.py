from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import app.trading_execution_canonical as canonical_module
from app.critical_entry_policy import parse_critical_entries
from app.mt5_execution_day26 import Day26Mt5ExecutionService, _SignalInput
from app.paper_critical_execution import PaperCriticalExecutionService
from app.trading_execution_canonical import (
    CanonicalTradingExecutionService,
    MemberTradingExecutionService,
)


def _signal() -> _SignalInput:
    return _SignalInput(
        signal_id=uuid4(),
        symbol="XAUUSD",
        side="BUY",
        entry_low=Decimal("4393"),
        entry_high=Decimal("4398"),
        stop_loss=Decimal("4392"),
        take_profits=(Decimal("4400"), Decimal("4403"), Decimal("4407")),
        has_open_runner=True,
        signal_requests_double_lot=False,
        source_revision_index=0,
        source_posted_at=datetime.now(UTC),
    )


def test_tdc_six_entries_keep_six_atomic_positions_not_twenty_four() -> None:
    raw = (
        "BUY GOLD @ 4398/4393\n\n"
        "TP 4400\nTP 4403\nTP 4407\nTP OPEN\nSL 4392\n\nHIGH RISK TRADE"
    )
    entries = parse_critical_entries(
        raw,
        side="BUY",
        entry_low="4393",
        entry_high="4398",
    )
    allocations = CanonicalTradingExecutionService._allocation_pairs(
        entries,
        (Decimal("4400"), Decimal("4403"), Decimal("4407"), None),
    )
    assert len(entries) == 6
    assert len(allocations) == 6
    assert {item.entry.entry_index for item in allocations} == {1, 2, 3, 4, 5, 6}


def test_demo_and_live_share_one_unmodified_risk_sizer() -> None:
    # There is one sizing implementation for normal, layered/pending, demo and LIVE.
    # No execution subclass may scale account balance by entry/TP count.
    assert "_full_risk_section_count" not in vars(canonical_module)
    assert "_size_signal" not in CanonicalTradingExecutionService.__dict__
    assert "_size_signal" not in MemberTradingExecutionService.__dict__
    assert "_size_signal" not in PaperCriticalExecutionService.__dict__
    assert CanonicalTradingExecutionService._size_signal is Day26Mt5ExecutionService._size_signal
    assert MemberTradingExecutionService._size_signal is Day26Mt5ExecutionService._size_signal
    assert PaperCriticalExecutionService._size_signal is Day26Mt5ExecutionService._size_signal


def test_six_entry_signal_uses_real_balance_once_for_each_atomic_leg() -> None:
    service = object.__new__(CanonicalTradingExecutionService)
    actual = CanonicalTradingExecutionService._size_signal(
        service,
        signal=_signal(),
        execution_entry=Decimal("4398"),
        balance=2000.0,
        price_loss_tick_value=1.0,
        specification={
            "minVolume": 0.01,
            "maxVolume": 100.0,
            "volumeStep": 0.01,
            "tickSize": 0.01,
        },
        risk_percent=Decimal("1"),
        double_lot_approved=False,
    )

    assert actual.balance == Decimal("2000.0")
    assert actual.effective_risk_percent == Decimal("1")
    assert actual.risk_budget_per_position == Decimal("20.0")
    assert actual.volume == Decimal("0.03")
    assert actual.actual_risk_per_position == Decimal("18.00")


def test_double_signal_uses_two_percent_of_real_balance_only() -> None:
    service = object.__new__(CanonicalTradingExecutionService)
    signal = _signal()
    object.__setattr__(signal, "signal_requests_double_lot", True)

    actual = CanonicalTradingExecutionService._size_signal(
        service,
        signal=signal,
        execution_entry=Decimal("4398"),
        balance=1500.0,
        price_loss_tick_value=1.0,
        specification={
            "minVolume": 0.01,
            "maxVolume": 100.0,
            "volumeStep": 0.01,
            "tickSize": 0.01,
        },
        risk_percent=Decimal("1"),
        double_lot_approved=True,
    )

    assert actual.effective_risk_percent == Decimal("2")
    assert actual.risk_budget_per_position == Decimal("30.0")
    assert actual.volume == Decimal("0.05")
    assert actual.actual_risk_per_position == Decimal("30.00")
