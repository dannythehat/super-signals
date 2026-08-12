"""Read-retry wrapper for the one-shot Day 27 live acceptance only.

Trade mutations are intentionally not retried. This wrapper only makes transient GET
reads resilient enough for a controlled Render acceptance run.
"""

from __future__ import annotations

import asyncio

import app.day27_live_acceptance as live_acceptance
from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_read_gateway import MetaApiReadGateway


class _RetryingMetaApiReadGateway(MetaApiReadGateway):
    async def _request(self, method: str, url: str, *, token: str):
        last: MetaApiGatewayError | None = None
        for attempt in range(1, 5):
            try:
                return await super()._request(method, url, token=token)
            except MetaApiGatewayError as exc:
                last = exc
                if not exc.retryable or attempt == 4:
                    raise
                await asyncio.sleep(2.0)
        assert last is not None
        raise last


async def run_day27_live_acceptance_with_read_retries() -> None:
    original = live_acceptance.MetaApiReadGateway
    live_acceptance.MetaApiReadGateway = _RetryingMetaApiReadGateway
    try:
        await live_acceptance.run_day27_live_acceptance()
    finally:
        live_acceptance.MetaApiReadGateway = original


__all__ = ["run_day27_live_acceptance_with_read_retries"]
