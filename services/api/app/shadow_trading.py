"""Compatibility surface for the Provider Lab shadow runtime.

Research follows the provider's current stop exactly. Provider Lab never applies the
live app's house breakeven/TP stop-management rules unless the provider explicitly sends
that management instruction.
"""

from app.shadow_trading_service_v4 import ShadowTradeService
from app.shadow_trading_v2 import _benchmark_pnl_usd, _decimal, _leg_r
from app.shadow_trading_v3 import ShadowTradeManager

__all__ = [
    "ShadowTradeManager",
    "ShadowTradeService",
    "_benchmark_pnl_usd",
    "_decimal",
    "_leg_r",
]
