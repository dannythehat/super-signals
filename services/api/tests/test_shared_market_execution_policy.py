from decimal import Decimal
from inspect import getsource

from app.critical_entry_policy import parse_critical_entries
from app.paper_fresh_start_execution import PaperFreshStartExecutionService


def test_fresh_market_zone_is_not_converted_to_critical_pending_structure() -> None:
    raw = (
        "Buy Gold @4395-4385\n\nSL: 4380\n\n"
        "TP1: 4399\nTP2: 4405\n\nEnter Slowly - Layer with proper money management"
    )
    entries = parse_critical_entries(
        raw,
        side="BUY",
        entry_low=Decimal("4385"),
        entry_high=Decimal("4395"),
    )
    assert entries == ()


def test_explicit_multi_entry_structure_remains_critical_broker_structure() -> None:
    raw = (
        "BUY GOLD @ 4391/4389\n\nTP 4393\nTP 4396\nTP 4400\n"
        "TP OPEN\nSL 4388\n\nHIGH RISK TRADE"
    )
    entries = parse_critical_entries(
        raw,
        side="BUY",
        entry_low=Decimal("4389"),
        entry_high=Decimal("4391"),
    )
    assert len(entries) > 1
    assert entries[0].order_type == "market"
    assert all(item.price > 0 for item in entries)


def test_shared_executor_routes_only_literal_multi_entry_or_pending_to_critical_path() -> None:
    source = getsource(PaperFreshStartExecutionService.execute_owner_demo_signal)
    assert 'critical.broad_order_type == "pending" or len(entries) > 1' in source
    assert "PaperCriticalExecutionService.execute_owner_demo_signal" in source
    assert "AtomicDay26Mt5ExecutionService.execute_owner_demo_signal" in source
