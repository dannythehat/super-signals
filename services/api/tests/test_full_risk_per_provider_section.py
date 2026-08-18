from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from app.critical_entry_policy import parse_critical_entries
from app.mt5_execution_day26 import _SignalInput
from app.paper_execution_priority import PaperExecutionPriorityService
from app.paper_fresh_start_execution import (
    PaperFreshStartExecutionService,
    _full_risk_section_count,
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


def test_tdc_six_sections_keep_six_atomic_positions_not_twenty_four() -> None:
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
    allocations = PaperFreshStartExecutionService._allocation_pairs(
        entries,
        (Decimal("4400"), Decimal("4403"), Decimal("4407"), None),
    )
    assert len(entries) == 6
    assert len(allocations) == 6
    assert {item.entry.entry_index for item in allocations} == {1, 2, 3, 4, 5, 6}


def test_six_section_signal_sizes_each_section_from_full_balance() -> None:
    service = object.__new__(PaperFreshStartExecutionService)
    signal = _signal()
    specification = {
        "minVolume": 0.01,
        "maxVolume": 100.0,
        "volumeStep": 0.01,
        "tickSize": 0.01,
    }
    full_balance = Decimal("2000")
    divided_balance = full_balance / Decimal("6")

    token = _full_risk_section_count.set(6)
    try:
        actual = PaperFreshStartExecutionService._size_signal(
            service,
            signal=signal,
            execution_entry=Decimal("4398"),
            balance=float(divided_balance),
            price_loss_tick_value=1.0,
            specification=specification,
            risk_percent=Decimal("1"),
            double_lot_approved=False,
        )
    finally:
        _full_risk_section_count.reset(token)

    expected = PaperExecutionPriorityService._size_signal(
        service,
        signal=signal,
        execution_entry=Decimal("4398"),
        balance=float(full_balance),
        price_loss_tick_value=1.0,
        specification=specification,
        risk_percent=Decimal("1"),
        double_lot_approved=False,
    )

    assert actual.risk_budget_per_position == expected.risk_budget_per_position
    assert actual.actual_risk_per_position == expected.actual_risk_per_position
    assert actual.volume == expected.volume
    assert actual.effective_risk_percent == Decimal("1")
    assert actual.risk_budget_per_position == Decimal("20")
