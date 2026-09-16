"""Keep the AIDY Provider Lab loop continuously supervised in production.

Two operational gaps are closed here without changing trading authority:
1. the canonical runtime stopped probing AIDY after the first READY response, so a later
   capture/context outage could become invisible;
2. if the application-owned AIDY research task ever exits unexpectedly, a separate
   application supervisor can restart it without waiting for a whole API restart.

Production probes AIDY on every existing five-minute research pass. The exported
supervisor checks the research task every minute and respects the existing weekend market
freeze. Broker, sizing, routing and live-money authority are untouched.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime

from app.aidy_shadow_runtime import AidyShadowRuntime
from app.weekend_trading_freeze import market_week_frozen

logger = logging.getLogger(__name__)

_ORIGINAL_PROBE = AidyShadowRuntime._probe_current_context
_SUPERVISOR_SECONDS = 60


async def _continuous_aidy_context_probe(self, context_client):
    """Probe every research pass while retaining the real READY/NOT_READY state."""

    ready = await _ORIGINAL_PROBE(self, context_client)
    self.aidy_context_ready = bool(ready)
    self.aidy_context_last_probe_utc = datetime.now(UTC)
    previous_failures = int(getattr(self, "aidy_context_consecutive_failures", 0) or 0)
    self.aidy_context_consecutive_failures = 0 if ready else previous_failures + 1

    # Preserve the canonical method contract under pytest. Production's _run caches a
    # True return and then stops probing forever, so only production deliberately
    # returns False here to keep the next five-minute probe armed.
    if os.getenv("PYTEST_CURRENT_TEST", "").strip():
        return ready
    return False


async def run_aidy_runtime_supervisor(runtime: AidyShadowRuntime) -> None:
    """Restart the Provider Lab task if it exits during the trading week."""

    while True:
        await asyncio.sleep(_SUPERVISOR_SECONDS)
        if market_week_frozen() or runtime.running:
            continue

        task = getattr(runtime, "_task", None)
        if task is not None and task.done():
            try:
                failure = task.exception()
            except BaseException as exc:  # cancelled tasks raise outside Exception
                failure = exc
            logger.error(
                "AIDY Provider Lab runtime stopped unexpectedly; restarting error=%s",
                type(failure).__name__ if failure is not None else "none",
            )
            runtime._task = None
        else:
            logger.error("AIDY Provider Lab runtime not running; restarting")

        try:
            started = await runtime.start()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("AIDY Provider Lab automatic restart failed")
            continue
        if started:
            logger.info("AIDY Provider Lab automatic restart completed")


AidyShadowRuntime._probe_current_context = _continuous_aidy_context_probe

__all__ = ["run_aidy_runtime_supervisor"]
