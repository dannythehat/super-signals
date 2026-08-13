"""API route modules.

Small route extensions are registered here before ``main`` imports their parent
routers, keeping invitation and invited-user controls isolated from startup code.
"""

from . import admin_accounts as _admin_accounts  # noqa: F401
from . import auth as _auth  # noqa: F401
from . import invitations as _invitations  # noqa: F401
from . import manual_reconciliation_day36 as _manual_reconciliation_day36
from . import mt5_approvals_day30 as _mt5_approvals_day30
from . import mt5_onboarding_day35 as _mt5_onboarding_day35
from . import registration as _registration  # noqa: F401
from . import trading_controls_day31 as _trading_controls_day31
from . import user_mt5_accounts as _user_mt5_accounts

_user_mt5_accounts.router.include_router(_trading_controls_day31.router)
_user_mt5_accounts.router.include_router(_mt5_onboarding_day35.user_router)
_user_mt5_accounts.router.include_router(_manual_reconciliation_day36.router)
_mt5_approvals_day30.router.include_router(_mt5_onboarding_day35.owner_router)
