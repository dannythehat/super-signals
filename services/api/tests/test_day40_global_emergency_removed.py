"""Day 40 regression for the Owner-locked removal of global emergency stop."""

from app.permissions import PERMISSION_CATALOG, ROLE_PERMISSIONS
from app.routes.admin_user_controls_day35 import router


def test_global_emergency_permission_is_not_exposed() -> None:
    assert "emergency_stop.use" not in PERMISSION_CATALOG
    assert "emergency_stop.use" not in ROLE_PERMISSIONS["owner"]
    assert "emergency_stop.use" not in ROLE_PERMISSIONS["trading_admin"]


def test_global_emergency_routes_are_not_registered() -> None:
    paths = {route.path for route in router.routes}
    assert "/user-controls/emergency-preview" not in paths
    assert "/user-controls/emergency-stop" not in paths
    assert "/user-controls/users" in paths
    assert "/user-controls/users/{user_id}/revoke" in paths
