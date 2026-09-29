"""Bounded retry wrapper for read-only MetaAPI broker state.

DEMO and LIVE execution use the same broker-read reliability policy. Trade mutations are
never retried here. GETs are idempotent and may be retried briefly on MetaAPI
429/5xx/network timeout responses before a fresh signal is abandoned. The execution
engine still re-checks signal freshness immediately before any broker mutation.

A MetaAPI cloud terminal can occasionally remain reported as DEPLOYED/CONNECTED by the
provisioning API while its region-scoped account-information endpoint stops responding.
After three genuine account-information timeouts this gateway requests one MetaAPI
terminal redeploy, then enters a long process-local cooldown. This restarts MetaAPI's
cloud terminal only; it never submits an MT5 trade and never relaxes signal freshness.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from urllib.parse import quote

import httpx

from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_read_gateway import DEFAULT_METAAPI_PROVISIONING_URL, MetaApiReadGateway

logger = logging.getLogger(__name__)

_ACCOUNT_INFORMATION_URL = re.compile(
    r"/users/current/accounts/([^/?]+)/account-information(?:\?|$)"
)
_TERMINAL_REDEPLOY_COOLDOWN_SECONDS = 30 * 60
_TERMINAL_REDEPLOY_TIMEOUT_SECONDS = 10.0
_LAST_TERMINAL_REDEPLOY_ATTEMPT: dict[str, float] = {}


def _account_information_account_id(url: str) -> str | None:
    match = _ACCOUNT_INFORMATION_URL.search(url or "")
    if match is None:
        return None
    value = match.group(1).strip()
    return value or None


class PaperResilientMetaApiReadGateway(MetaApiReadGateway):
    """Shared bounded retry policy for read-only MetaAPI requests."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 5.0,
        attempts: int = 3,
        retry_delay_seconds: float = 0.25,
    ) -> None:
        super().__init__(timeout_seconds=timeout_seconds)
        # Production dashboard wiring historically passed attempts=1 to keep reads
        # snappy. That made a single MetaAPI 429/5xx/network blip erase the whole live
        # account snapshot (balance, equity and current position prices) even though
        # the next request often succeeded seconds later. Read-only GETs are safe to
        # retry, so never allow fewer than three bounded attempts here. Trade/order
        # mutations do not use this gateway and are never retried by this policy.
        self._paper_read_attempts = max(3, int(attempts))
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
                    if exc.code == "metaapi_timeout":
                        account_id = _account_information_account_id(url)
                        if account_id is not None:
                            await self._redeploy_stalled_terminal(
                                token=token,
                                account_id=account_id,
                            )
                    raise
                logger.warning(
                    "Retrying MetaAPI read attempt=%d/%d code=%s",
                    attempt,
                    self._paper_read_attempts,
                    exc.code,
                )
                if self._paper_retry_delay_seconds:
                    await asyncio.sleep(self._paper_retry_delay_seconds * attempt)

        assert last_error is not None
        raise last_error

    async def _redeploy_stalled_terminal(self, *, token: str, account_id: str) -> None:
        """Request one bounded MetaAPI terminal restart after sustained read timeouts.

        The cooldown is marked before the network call so concurrent failing readers in
        the same process cannot create a redeploy storm. A failed redeploy request also
        consumes the cooldown; an upstream outage must not cause aggressive retries.
        """
        now = time.monotonic()
        previous = _LAST_TERMINAL_REDEPLOY_ATTEMPT.get(account_id)
        if previous is not None and now - previous < _TERMINAL_REDEPLOY_COOLDOWN_SECONDS:
            return
        _LAST_TERMINAL_REDEPLOY_ATTEMPT[account_id] = now

        encoded_account = quote(account_id, safe="")
        url = (
            f"{DEFAULT_METAAPI_PROVISIONING_URL}/users/current/accounts/"
            f"{encoded_account}/redeploy"
        )
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(_TERMINAL_REDEPLOY_TIMEOUT_SECONDS)
            ) as client:
                response = await client.post(
                    url,
                    headers={"Accept": "application/json", "auth-token": token},
                )
        except httpx.HTTPError:
            logger.warning(
                "MetaAPI terminal self-heal redeploy request failed after repeated read timeouts"
            )
            return

        if response.status_code in {200, 201, 202, 204}:
            logger.warning(
                "Requested one-shot MetaAPI terminal redeploy after repeated account-information timeouts"
            )
            return

        logger.warning(
            "MetaAPI terminal self-heal redeploy rejected status=%s",
            response.status_code,
        )


# Neutral name for new wiring while retaining the original import for compatibility.
ResilientMetaApiReadGateway = PaperResilientMetaApiReadGateway


__all__ = ["PaperResilientMetaApiReadGateway", "ResilientMetaApiReadGateway"]
