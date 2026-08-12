"""API route modules.

Small route extensions are registered here before ``main`` imports their parent
routers, keeping invitation and invited-user controls isolated from startup code.
"""

from . import admin_accounts as _admin_accounts  # noqa: F401
from . import auth as _auth  # noqa: F401
from . import invitations as _invitations  # noqa: F401
from . import registration as _registration  # noqa: F401
from . import user_mt5_accounts as _user_mt5_accounts
from . import trading_controls_day31 as _trading_controls_day31

_user_mt5_accounts.router.include_router(_trading_controls_day31.router)
