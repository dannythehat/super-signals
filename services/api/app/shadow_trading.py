"""Compatibility surface for the Provider Lab shadow runtime."""

from app.shadow_trading_v2 import (
    ShadowTradeManager,
    ShadowTradeService,
    _benchmark_pnl_usd,
    _decimal,
    _leg_r,
)

__all__ = [
    "ShadowTradeManager",
    "ShadowTradeService",
    "_benchmark_pnl_usd",
    "_decimal",
    "_leg_r",
]
