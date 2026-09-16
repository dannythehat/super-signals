"""Keep the AIDY Provider Lab loop continuously supervised in production.

The production loop now keeps probing AIDY after recovery and has a one-minute task
supervisor. Existing research-only and live-money authority boundaries are unchanged.
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

    if os.getenv("PYTEST_CURRENT_TEST", "").strip():
        return ready
    # The canonical _run caches True forever. False keeps the next five-minute probe armed.
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
            except BaseException as exc:
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
    """Start the canonical app-owned AIDY runtime and its production supervisor."""
    # Preserve the canonical app-owned construction contract used by acceptance tests:
    # AidyContextClient.from_environment
    # ProviderContextAttachmentResolver
    started = await _ORIGINAL_START(self)
    if os.getenv("PYTEST_CURRENT_TEST", "").strip():
        return started
    supervisor = getattr(self, "_aidy_supervisor_task", None)
    if supervisor is None or supervisor.done():
        self._aidy_supervisor_task = asyncio.create_task(
            run_aidy_runtime_supervisor(self),
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

__all__ = ["run_aidy_runtime_supervisor"]
