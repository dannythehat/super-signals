import inspect

from app.mt5_execution_day38 import Day38LiveUserExecutionService
from app.mt5_management_day38 import Day38LiveUserManagementService
from app.paper_critical_management_v2 import PaperCriticalManagementV2
from app.paper_fresh_start_execution import PaperFreshStartExecutionService
from app.paper_safe_member_routing import (
    PaperSafeMemberDistribution,
    PaperSafeMemberManagement,
)


def test_tdc_layered_execution_is_not_a_paper_only_structure() -> None:
    assert issubclass(Day38LiveUserExecutionService, PaperFreshStartExecutionService)
    source = inspect.getsource(PaperSafeMemberDistribution)
    assert "critical_structure_paper_only" not in source
    assert "_critical_signal" not in source


def test_layer_management_is_not_blocked_from_future_live_engine() -> None:
    assert issubclass(Day38LiveUserManagementService, PaperCriticalManagementV2)
    source = inspect.getsource(PaperSafeMemberManagement)
    assert "critical_management_paper_only" not in source
    assert "_critical_event" not in source


def test_live_boundary_is_one_global_switch_not_signal_shape() -> None:
    distribution_source = inspect.getsource(PaperSafeMemberDistribution)
    management_source = inspect.getsource(PaperSafeMemberManagement)
    assert "live_execution_enabled" in distribution_source
    assert "live_execution_enabled" in management_source
