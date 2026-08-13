"""Day 35 role/privacy contract across admin and invited-user surfaces."""

from __future__ import annotations

import inspect

from app.performance_ledger_day33 import Day33PerformanceLedgerService
from app.routes import admin_user_controls_day35, performance_day33


def test_provider_identity_is_admin_only_in_canonical_timeline() -> None:
    source = inspect.getsource(Day33PerformanceLedgerService.read_timeline)

    assert 'provider_visible = viewer_role in {"owner", "trading_admin"}' in source
    assert 'viewer_role="user"' not in source


def test_signal_portfolio_rejects_non_admin_roles() -> None:
    source = inspect.getsource(performance_day33._admin_portfolio_role)

    assert '{"owner", "trading_admin"}' in source
    assert "HTTP_403_FORBIDDEN" in source


def test_member_revoke_is_owner_permission_only() -> None:
    source = inspect.getsource(admin_user_controls_day35)

    assert 'require_permission("users.manage")' in source
    assert 'require_permission("emergency_stop.use")' in source


def test_member_list_and_revoke_routes_use_owner_guard() -> None:
    source = inspect.getsource(admin_user_controls_day35.managed_users)
    revoke_source = inspect.getsource(admin_user_controls_day35.revoke_user)

    assert "actor: OwnerUsers" in source
    assert "actor: OwnerUsers" in revoke_source


def test_emergency_stop_uses_shared_admin_emergency_permission() -> None:
    preview_source = inspect.getsource(admin_user_controls_day35.emergency_preview)
    execute_source = inspect.getsource(admin_user_controls_day35.emergency_stop)

    assert "actor: EmergencyAdmin" in preview_source
    assert "actor: EmergencyAdmin" in execute_source
