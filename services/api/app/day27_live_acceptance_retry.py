"""Read/preflight-retry wrapper for the one-shot Day 27 live acceptance only.

Trade mutations are intentionally not retried. This wrapper makes only transient
MetaAPI terminal GETs and the non-trading calculate-margin preflight resilient enough
for a controlled Render acceptance run.
"""

from __future__ import annotations

import asyncio

import app.day27_live_acceptance as live_acceptance
from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_margin_gateway import MetaApiMarginGateway
from app.metaapi_read_gateway import MetaApiReadGateway


async def _backoff(attempt: int) -> None:
    await asyncio.sleep(min(2.0 * attempt, 6.0))


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
                await _backoff(attempt)
        assert last is not None
        raise last


class _RetryingMetaApiMarginGateway(MetaApiMarginGateway):
    async def _request(
        self,
        method: str,
        url: str,
        *,
        token: str,
        json_body: dict[str, object],
    ):
        last: MetaApiGatewayError | None = None
        for attempt in range(1, 5):
            try:
                return await super()._request(
                    method,
                    url,
                    token=token,
                    json_body=json_body,
                )
            except MetaApiGatewayError as exc:
                last = exc
                if not exc.retryable or attempt == 4:
                    raise
                await _backoff(attempt)
        assert last is not None
        raise last


async def run_day27_live_acceptance_with_read_retries() -> None:
    original_read = live_acceptance.MetaApiReadGateway
    original_margin = live_acceptance.MetaApiMarginGateway
    live_acceptance.MetaApiReadGateway = _RetryingMetaApiReadGateway
    live_acceptance.MetaApiMarginGateway = _RetryingMetaApiMarginGateway
    try:
        await live_acceptance.run_day27_live_acceptance()
    finally:
        live_acceptance.MetaApiReadGateway = original_read
        live_acceptance.MetaApiMarginGateway = original_margin


__all__ = ["run_day27_live_acceptance_with_read_retries"]
