"""One-time PIT-safe recovery of Provider Context v1 terminal misses.

This job is research-only and broker-isolated. It replays historical signal timestamps
through AIDY's current Provider Context contract. A v1 miss is preserved forever; the
v2 result is either a canonical immutable attachment or a second terminal miss proving
the signal is still not PIT-valid under the new D1-only degraded-context policy.
"""

from __future__ import annotations

import asyncio
import json

from app.aidy_context_client import AidyContextClient
from app.db import get_session_factory
from app.provider_context_attachment_v2 import ProviderContextAttachmentResolverV2

_MAX_PASSES = 200


async def recover() -> dict[str, int | bool]:
    client = AidyContextClient.from_environment()
    if client is None:
        summary: dict[str, int | bool] = {
            "configured": False,
            "passes": 0,
            "attached": 0,
            "terminal_misses": 0,
            "failures": 0,
        }
        print("PROVIDER_CONTEXT_V2_RECOVERY=" + json.dumps(summary, sort_keys=True), flush=True)
        return summary

    resolver = ProviderContextAttachmentResolverV2(get_session_factory(), client)
    total_attached = 0
    total_terminal = 0
    total_failures = 0
    passes = 0
    for _ in range(_MAX_PASSES):
        passes += 1
        attached, failures = await resolver.resolve_once()
        terminal = resolver.last_terminal_misses
        total_attached += attached
        total_terminal += terminal
        total_failures += failures
        # A full batch has been drained when this pass made no durable progress.
        # Transient failures remain available to the normal application resolver.
        if attached == 0 and terminal == 0:
            break
        await asyncio.sleep(0)

    summary = {
        "configured": True,
        "passes": passes,
        "attached": total_attached,
        "terminal_misses": total_terminal,
        "failures": total_failures,
    }
    print("PROVIDER_CONTEXT_V2_RECOVERY=" + json.dumps(summary, sort_keys=True), flush=True)
    return summary


def run() -> dict[str, int | bool]:
    return asyncio.run(recover())


if __name__ == "__main__":
    run()
