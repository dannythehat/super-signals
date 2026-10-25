"""One-shot independent read-only readiness check for the PU Prime LIVE master.

Runs in a background task, never touches the Vantage route or opens trades.
No MT5 credentials or bridge tokens are logged.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from app.local_bridge_queue import LocalBridgeQueue
from app.pu_broker_readiness import assess_broker_preflight

logger = logging.getLogger(__name__)


async def check_pu_master_once(session_factory: Any) -> None:
    account_id = os.getenv("SUPER_SIGNALS_PU_BRIDGE_ACCOUNT_ID", "").strip()
    profile = os.getenv("SUPER_SIGNALS_PU_BRIDGE_PROFILE", "pu-prime").strip()
    if not account_id or not profile:
        logger.warning("PU MASTER PREFLIGHT skipped: PU worker profile/account not configured")
        return

    queue = LocalBridgeQueue(
        session_factory, profile=profile, account_id=account_id,
        timeout_seconds=15.0,
    )
    operations = (
        ("read_terminal_identity", {}),
        ("read_account_information", {}),
        ("read_symbol_specification", {"symbol": "XAUUSD.s"}),
        ("read_positions", {}),
        ("read_orders", {}),
    )
    # A restarting worker may be online a few seconds after app startup.
    # Retry only read operations, never any broker mutation.
    for attempt in range(1, 4):
        try:
            results: dict[str, Any] = {}
            for operation, payload in operations:
                results[operation] = await queue.execute(
                    account_id=account_id, operation=operation, payload=payload,
                )
            report = assess_broker_preflight(
                identity=results["read_terminal_identity"],
                account=results["read_account_information"],
                symbol=results["read_symbol_specification"],
                positions=results["read_positions"],
                orders=results["read_orders"],
            )
            logger.warning(
                "PU MASTER PREFLIGHT status=%s blockers=%s login=%s "
                "balance=%s equity=%s currency=%s gold=%s min_volume=%s "
                "live_trade_route_verified=%s",
                report["broker_preflight"],
                report["problems"],
                report["source_login"],
                report["account"]["balance"],
                report["account"]["equity"],
                report["account"]["currency"],
                report["gold_symbol"],
                report["symbol"]["min_volume"],
                report["actual_provider_order_routing_verified"],
            )
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "PU MASTER PREFLIGHT read failed attempt=%d/3 type=%s",
                attempt, type(exc).__name__,
            )
            if attempt < 3:
                await asyncio.sleep(8)
