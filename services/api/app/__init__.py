"""Super Signals API package.

Production behaviour is owned by explicitly constructed canonical services and classes.
The package also installs a narrow logging safety guard for third-party websocket
transport libraries. Those libraries can emit every MetaAPI price tick at INFO level,
which is high-volume diagnostic noise rather than an application event.
"""

import logging
import re


# MetaAPI uses python-engineio/python-socketio underneath. Their client loggers can
# print entire XAUUSD tick/synchronization payloads many times per second when the
# application's normal INFO logging is enabled. Disable only those raw transport
# loggers; application warnings/errors and our own execution diagnostics remain live.
for _transport_logger_name in ("engineio.client", "socketio.client"):
    _transport_logger = logging.getLogger(_transport_logger_name)
    _transport_logger.setLevel(logging.WARNING)
    _transport_logger.propagate = False


# Provider close-language hardening. FXTradingVision uses imperative wording such as
# "Close your gold sells" before issuing a counter-trade. This is mechanically explicit
# broker-management language and must never be downgraded to unsupported_management.
# Extend the canonical Day27 close matcher without weakening any of its fail-closed
# handling for result-only, optional, partial or profit-qualified text.
from app import day27_management_policy as _day27_management_policy

_day27_management_policy._EXIT_NOW = re.compile(
    _day27_management_policy._EXIT_NOW.pattern
    + r"|\bCLOSE\s+(?:(?:YOUR|MY|OUR|THE|ALL)\s+)?(?:(?:GOLD|XAU\s*(?:/\s*)?USD)\s+)(?:BUYS?|SELLS?)\b",
    re.IGNORECASE,
)
