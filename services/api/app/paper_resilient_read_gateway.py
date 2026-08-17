"""Bounded retry wrapper for Owner DEMO MetaAPI reads only.

Trade mutations are intentionally not retried here. GETs are idempotent and may be
retried briefly on MetaAPI 429/5xx/network timeout responses before a fresh signal is
abandoned. The execution service re-checks signal freshness immediately before any
broker mutation.
"""

from __future__ import annotations

import asyncio
import logging

from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_read_gateway import MetaApiReadGateway

logger = logging.getLogger(__name__)


class PaperResilientMetaApiReadGateway(MetaApiReadGateway):
    """Retry only retryable read-only MetaAPI requests on the DEMO paper boundary."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 5.0,
        attempts: int = 3,
        retry_delay_seconds: float = 0.25,
    ) -> None:
        super().__init__(timeout_seconds=timeout_seconds)
        self._paper_read_attempts = max(1, int(attempts))
        self._paper_retry_delay_seconds = max(0.0, float(retry_delay_seconds))

    async def _request(self, method: str, url: str, *, token: str):
        if method.strip().upper() != "GET":
            return await super()._request(method, url, token=token)

        last_error: MetaApiGatewayError | None = None
        for attempt in range(1, self._paper_read_attempts + 1):
            try:
                return await super()._request(method, url, token=token)
            except MetaApiGatewayError as exc:
                last_error = exc
                if not exc.retryable or attempt >= self._paper_read_attempts:
                    raise
                logger.warning(
                    "Retrying DEMO MetaAPI read attempt=%d/%d code=%s",
                    attempt,
                    self._paper_read_attempts,
                    exc.code,
                )
                if self._paper_retry_delay_seconds:
                    await asyncio.sleep(self._paper_retry_delay_seconds * attempt)

        assert last_error is not None
        raise last_error


__all__ = ["PaperResilientMetaApiReadGateway"]
