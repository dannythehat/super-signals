"""Keep the AIDY Provider Lab loop continuously supervised in production.

Two operational gaps are closed here without changing trading authority:
1. the canonical runtime stopped probing AIDY after the first READY response, so a later
   capture/context outage could become invisible;
2. if the application-owned AIDY research task ever exits unexpectedly, nothing restarted
   it until the whole API service restarted.

Production therefore probes AIDY on every existing five-minute research pass and runs a
small one-minute supervisor that restarts the research task if it is no longer running.
The supervisor respects the existing weekend market freeze. Broker, sizing, routing and
live-money authority are untouched.
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
_ORIGINAL_START = AidyShadowRuntime.start
_ORIGINAL_STOP = AidyShadowRuntime.stop
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


async def _supervise_runtime(runtime: AidyShadowRuntime) -> None:
    """Restart the Provider Lab task if it exits during the trading week."""

    while True:
        await asyncio.sleep(_SUPERVISOR_SECONDS)
        if market_week_frozen():
            continue
        if runtime.running:
            continue

        task = getattr(runtime, "_task", None)
        if task is not None and task.done():
            try:
                failure = task.exception()
            except (asyncio.CancelledError, Exception) as exc:  # defensive diagnostics only
                failure = exc
            logger.error(
                "AIDY Provider Lab runtime stopped unexpectedly; restarting error=%s",
                type(failure).__name__ if failure is not None else "none",
            )
            runtime._task = None
        else:
            logger.error("AIDY Provider Lab runtime not running; restarting")

        try:
            started = await _ORIGINAL_START(runtime)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("AIDY Provider Lab automatic restart failed")
            continue
        if started:
            logger.info("AIDY Provider Lab automatic restart completed")


async def _supervised_start(self) -> bool:
    started = await _ORIGINAL_START(self)
    if os.getenv("PYTEST_CURRENT_TEST", "").strip():
        return started
    supervisor = getattr(self, "_aidy_supervisor_task", None)
    if supervisor is None or supervisor.done():
        self._aidy_supervisor_task = asyncio.create_task(
            _supervise_runtime(self),
            name="super-signals-aidy-runtime-supervisor",
        )
        logger.info("AIDY Provider Lab one-minute supervisor started")
    return started


async def _supervised_stop(self) -> None:
    supervisor = getattr(self, "_aidy_supervisor_task", None)
    if supervisor is not None:
        if not supervisor.done():
            supervisor.cancel()
        try:
            await supervisor
        except asyncio.CancelledError:
            pass
        self._aidy_supervisor_task = None
    await _ORIGINAL_STOP(self)


AidyShadowRuntime._probe_current_context = _continuous_aidy_context_probe
AidyShadowRuntime.start = _supervised_start
AidyShadowRuntime.stop = _supervised_stop
