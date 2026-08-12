"""API route modules.

Day 29 extends the existing owner-admin and authentication routers in separate
small modules. Importing them here registers those routes before ``main`` imports
the router objects, keeping invitation work isolated from the trading startup.
"""

from . import admin_accounts as _admin_accounts  # noqa: F401
from . import auth as _auth  # noqa: F401
from . import invitations as _invitations  # noqa: F401
from . import registration as _registration  # noqa: F401
