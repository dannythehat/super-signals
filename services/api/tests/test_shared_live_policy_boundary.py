import inspect

from app.member_routing_canonical import (
    MemberDistributionService,
    MemberManagementService,
    live_execution_enabled,
)
from app.trading_execution_canonical import (
    CanonicalTradingExecutionService,
    MemberTradingExecutionService,
)
from app.trading_management_canonical import (
    CanonicalTradingManagementService,
    MemberTradingManagementService,
)


def test_layered_execution_is_not_paper_only_structure() -> None:
    assert issubclass(MemberTradingExecutionService, CanonicalTradingExecutionService)
    source = inspect.getsource(MemberDistributionService)
    assert "critical_structure_paper_only" not in source
    assert "_critical_signal" not in source


def test_layer_management_is_not_blocked_from_future_live_engine() -> None:
    assert issubclass(MemberTradingManagementService, CanonicalTradingManagementService)
    source = inspect.getsource(MemberManagementService)
    assert "critical_management_paper_only" not in source
    assert "_critical_event" not in source


def test_live_boundary_is_one_global_switch_not_signal_shape(monkeypatch) -> None:
    distribution_source = inspect.getsource(MemberDistributionService)
    management_source = inspect.getsource(MemberManagementService)
    assert "live_execution_enabled" in distribution_source
    assert "live_execution_enabled" in management_source

    monkeypatch.delenv("SUPER_SIGNALS_LIVE_EXECUTION_ENABLED", raising=False)
    assert live_execution_enabled() is False
    monkeypatch.setenv("SUPER_SIGNALS_LIVE_EXECUTION_ENABLED", "1")
    assert live_execution_enabled() is True
