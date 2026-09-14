"""API route modules.

Small route extensions are registered here before ``main`` imports their parent
routers. Member MT5 onboarding/reconnect is canonicalized through Connection V2;
legacy routes remain available only for compatibility and are not used by the Owner UI.
"""

from . import admin_accounts as _admin_accounts  # noqa: F401
from . import auth as _auth  # noqa: F401
from . import complimentary_access as _complimentary_access
from . import invitations as _invitations  # noqa: F401
from . import manual_reconciliation_day36 as _manual_reconciliation_day36
from . import member_connection_v2 as _member_connection_v2
from . import member_onboarding_owner as _member_onboarding_owner  # noqa: F401
from . import member_subscription_owner_hotfix as _member_subscription_owner_hotfix
from . import mt5_approvals_day30 as _mt5_approvals_day30
from . import owner_manual_close as _owner_manual_close
from . import public_registration as _public_registration  # noqa: F401
from . import registration as _registration  # noqa: F401
from . import subscriptions as _subscriptions
from . import trading_controls_day31 as _trading_controls_day31
from . import user_mt5_accounts as _user_mt5_accounts

_admin_accounts.router.include_router(_member_connection_v2.router)
_subscriptions.owner_router.include_router(_complimentary_access.router)
_user_mt5_accounts.router.include_router(_subscriptions.user_router)
_user_mt5_accounts.router.include_router(_trading_controls_day31.router)
_user_mt5_accounts.router.include_router(_manual_reconciliation_day36.router)
_user_mt5_accounts.router.include_router(_owner_manual_close.router)
_mt5_approvals_day30.router.include_router(_member_subscription_owner_hotfix.router)
_mt5_approvals_day30.router.include_router(_subscriptions.owner_router)
