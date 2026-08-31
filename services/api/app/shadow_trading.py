"""Compatibility surface for the Provider Lab shadow runtime."""

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
