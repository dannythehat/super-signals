"""Read-only broker preflight for the single PU Prime MT5 master account.

This is intentionally NOT an automatic-trading-ready signal: provider
dispatch, trade management and copier mirroring must be verified separately.
"""
from __future__ import annotations

from typing import Any


def assess_broker_preflight(
    *,
    identity: dict[str, Any],
    account: dict[str, Any],
    symbol: dict[str, Any],
    positions: list[Any],
    orders: list[Any],
    expected_login: int = 36394935,
    expected_server: str = "PUPrime-Live 6",
) -> dict[str, Any]:
    problems: list[str] = []
    if identity.get("login") != expected_login:
        problems.append("unexpected_mt5_login")
    if identity.get("server") != expected_server:
        problems.append("unexpected_mt5_server")
    if identity.get("goldSymbol") != "XAUUSD.s" or symbol.get("symbol") != "XAUUSD.s":
        problems.append("unexpected_gold_symbol")
    try:
        equity = float(account["equity"])
        balance = float(account["balance"])
        min_volume = float(symbol["minVolume"])
        volume_step = float(symbol["volumeStep"])
    except (KeyError, ValueError, TypeError):
        problems.append("invalid_broker_account_or_symbol_data")
        equity = balance = 0.0
        min_volume = volume_step = 0.0
    if not account.get("tradeAllowed") is True:
        problems.append("trading_not_allowed")
    if equity <= 0 or balance <= 0:
        problems.append("broker_account_not_funded")
    if min_volume <= 0 or volume_step <= 0:
        problems.append("invalid_minimum_order_size")
    if not isinstance(positions, list) or not isinstance(orders, list):
        problems.append("invalid_broker_exposure_response")
        positions = orders = []

    return {
        "broker_preflight": "passed" if not problems else "blocked",
        "problems": problems,
        "source_login": identity.get("login"),
        "source_server": identity.get("server"),
        "gold_symbol": symbol.get("symbol"),
        "account": {
            "balance": balance,
            "equity": equity,
            "currency": account.get("currency"),
            "trade_allowed": account.get("tradeAllowed") is True,
        },
        "symbol": {"min_volume": min_volume, "volume_step": volume_step},
        "open_broker_positions": len(positions),
        "open_broker_orders": len(orders),
        "actual_provider_order_routing_verified": False,
        "pu_prime_copy_follower_test_verified": False,
        "note": "Read-only broker checks; no live order or follower-copy verification",
    }
