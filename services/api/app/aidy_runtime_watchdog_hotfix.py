"""Keep AIDY Provider Context health probing continuous in production.

The canonical Provider Lab runtime originally stopped probing AIDY once one READY
probe succeeded. That allowed a later AIDY capture/context outage to go unnoticed by
the application-owned research loop. This narrow runtime patch keeps the existing
five-minute poll cadence and all existing fail-flat research behaviour, but forces the
probe to run on every loop pass. It does not grant broker, sizing, routing or live-money
authority.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.aidy_shadow_runtime import AidyShadowRuntime


_ORIGINAL_PROBE = AidyShadowRuntime._probe_current_context


async def _continuous_aidy_context_probe(self, context_client):
    """Probe every research pass while retaining the real READY/NOT_READY state."""

    ready = await _ORIGINAL_PROBE(self, context_client)
    self.aidy_context_ready = bool(ready)
    self.aidy_context_last_probe_utc = datetime.now(UTC)
    previous_failures = int(getattr(self, "aidy_context_consecutive_failures", 0) or 0)
    self.aidy_context_consecutive_failures = 0 if ready else previous_failures + 1

    # AidyShadowRuntime._run caches a True return and then stops probing forever.
    # Returning False here is deliberate: it keeps the next scheduled probe armed.
    return False


AidyShadowRuntime._probe_current_context = _continuous_aidy_context_probe
