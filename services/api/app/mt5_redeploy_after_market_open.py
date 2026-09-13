"""Run one-time live MT5 recovery only when the canonical weekly market is open."""

from __future__ import annotations

import asyncio
import logging
import os

from app.mt5_redeploy_once import main as run_redeploy
from app.weekend_trading_freeze import market_week_frozen

logger = logging.getLogger(__name__)


async def main() -> None:
    email = os.getenv("SMART_SIGNALS_ONE_TIME_MT5_REDEPLOY_EMAIL", "").strip()
    if not email:
        return

    if market_week_frozen():
        logger.info("One-time MT5 redeploy queued for canonical Monday market reopen")
        while market_week_frozen():
            await asyncio.sleep(30)

    await run_redeploy()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
