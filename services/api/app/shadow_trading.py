"""Compatibility surface for the Provider Lab shadow runtime.

Research follows the provider's current stop exactly. Provider Lab never applies the
live app's house breakeven/TP stop-management rules unless the provider explicitly sends
that management instruction. Provider Lab market observation is also isolated from the
owner's MetaAPI connection; the runtime uses the shared public XAUUSD research feed.
"""

from app.shadow_trading_service_v4 import ShadowTradeService
from app.shadow_trading_v2 import _benchmark_pnl_usd, _decimal, _leg_r
from app.shadow_trading_v5 import ShadowTradeManager

__all__ = [
    "ShadowTradeManager",
    "ShadowTradeService",
    "_benchmark_pnl_usd",
    "_decimal",
    "_leg_r",
]
