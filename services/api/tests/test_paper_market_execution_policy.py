from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.mt5_execution_day26_atomic import AtomicDay26Mt5ExecutionService
from app.paper_critical_execution import PaperCriticalExecutionService
from app.paper_execution_priority import PaperExecutionPriorityService
from app.paper_market_execution_policy import install_paper_market_execution_policy


@pytest.mark.asyncio
async def test_fresh_united_kings_market_zone_bypasses_zone_submission_veto(monkeypatch) -> None:
    install_paper_market_execution_policy()
    service = object.__new__(PaperExecutionPriorityService)
    signal_id = uuid4()
    critical = SimpleNamespace(
        original_text=(
            "Buy Gold @4395-4385\n\nSL: 4380\n\n"
            "TP1: 4399\nTP2: 4405\n\nEnter Slowly - Layer with proper money management"
        ),
        broad_order_type="market",
        base=SimpleNamespace(
            side="BUY",
            entry_low=Decimal("4385"),
            entry_high=Decimal("4395"),
        ),
    )
    monkeypatch.setattr(
        PaperExecutionPriorityService,
        "_load_critical_signal",
        lambda self, value: critical,
    )

    calls: list[str] = []

    async def atomic(self, **kwargs):
        calls.append("atomic")
        return "executed-at-live-price"

    async def critical_path(self, **kwargs):
        calls.append("critical")
        raise AssertionError("ordinary fresh market zone must not enter critical zone path")

    monkeypatch.setattr(AtomicDay26Mt5ExecutionService, "execute_owner_demo_signal", atomic)
    monkeypatch.setattr(PaperCriticalExecutionService, "execute_owner_demo_signal", critical_path)

    result = await service.execute_owner_demo_signal(
        owner_user_id=uuid4(),
        signal_id=signal_id,
        risk_percent="1",
        double_lot_approved=True,
    )

    assert result == "executed-at-live-price"
    assert calls == ["atomic"]


@pytest.mark.asyncio
async def test_explicit_multi_entry_structure_keeps_critical_broker_path(monkeypatch) -> None:
    install_paper_market_execution_policy()
    service = object.__new__(PaperExecutionPriorityService)
    critical = SimpleNamespace(
        original_text=(
            "BUY GOLD @ 4391/4389\n\nTP 4393\nTP 4396\nTP 4400\n"
            "TP OPEN\nSL 4388\n\nHIGH RISK TRADE"
        ),
        broad_order_type="market",
        base=SimpleNamespace(
            side="BUY",
            entry_low=Decimal("4389"),
            entry_high=Decimal("4391"),
        ),
    )
    monkeypatch.setattr(
        PaperExecutionPriorityService,
        "_load_critical_signal",
        lambda self, value: critical,
    )

    calls: list[str] = []

    async def atomic(self, **kwargs):
        calls.append("atomic")
        raise AssertionError("multi-entry signal must preserve critical broker semantics")

    async def critical_path(self, **kwargs):
        calls.append("critical")
        return "critical-executed"

    monkeypatch.setattr(AtomicDay26Mt5ExecutionService, "execute_owner_demo_signal", atomic)
    monkeypatch.setattr(PaperCriticalExecutionService, "execute_owner_demo_signal", critical_path)

    result = await service.execute_owner_demo_signal(
        owner_user_id=uuid4(),
        signal_id=uuid4(),
        risk_percent="1",
        double_lot_approved=True,
    )

    assert result == "critical-executed"
    assert calls == ["critical"]
