"""Read-only MetaAPI terminal data gateway for Day 23+ trading gates.

This module exposes account-state, position, open-order, quote, symbol-specification
and broker-history reads only. There is no trade/order mutation method here.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

import httpx

from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_region_cache import (
    get_metaapi_region,
    normalize_metaapi_region,
    remember_metaapi_region,
)

DEFAULT_METAAPI_PROVISIONING_URL = (
    "https://mt-provisioning-api-v1.agiliumtrade.agiliumtrade.ai"
)


class MetaApiReadGateway:
    """Small read-only client for MetaAPI terminal and market state."""

    def __init__(self, *, timeout_seconds: float = 30.0) -> None:
        self._timeout = httpx.Timeout(timeout_seconds)

    async def resolve_account_region(self, *, token: str, account_id: str) -> str:
        # Region is account metadata, not market data. The provisioning connection
        # monitor learns it during startup reconciliation before the Telegram reader
        # begins listening. Reuse that process-wide value here so a live trade does
        # not depend on an extra provisioning round-trip just to rediscover which
        # MetaAPI terminal hostname to call.
        cached = get_metaapi_region(account_id)
        if cached:
            return cached
        response = await self._request(
            "GET",
            f"{DEFAULT_METAAPI_PROVISIONING_URL}/users/current/accounts/{account_id}",
            token=token,
        )
        payload = self._json(response)
        if not isinstance(payload, dict):
            raise MetaApiGatewayError("metaapi_invalid_response")
        region = remember_metaapi_region(
            account_id,
            str(payload.get("region") or ""),
        )
        if region is None:
            raise MetaApiGatewayError("metaapi_region_unavailable")
        return region

    async def read_account_information(
        self, *, token: str, account_id: str, region: str
    ) -> dict[str, object]:
        payload = await self._read_terminal_json(
            token=token,
            region=region,
            path=f"/users/current/accounts/{account_id}/account-information",
        )
        if not isinstance(payload, dict):
            raise MetaApiGatewayError("metaapi_invalid_response")
        return payload

    async def read_positions(
        self, *, token: str, account_id: str, region: str
    ) -> list[dict[str, object]]:
        payload = await self._read_terminal_json(
            token=token,
            region=region,
            path=f"/users/current/accounts/{account_id}/positions",
        )
        if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
            raise MetaApiGatewayError("metaapi_invalid_response")
        return payload

    async def read_orders(
        self, *, token: str, account_id: str, region: str
    ) -> list[dict[str, object]]:
        """Read currently active terminal orders for Day 27 pending-order cancellation."""
        payload = await self._read_terminal_json(
            token=token,
            region=region,
            path=f"/users/current/accounts/{account_id}/orders",
        )
        if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
            raise MetaApiGatewayError("metaapi_invalid_response")
        return payload

    async def read_deals_by_position(
        self,
        *,
        token: str,
        account_id: str,
        region: str,
        position_id: str,
    ) -> list[dict[str, object]]:
        """Read immutable MT5 deal history for one broker position."""
        encoded_position = quote(position_id, safe="")
        payload = await self._read_terminal_json(
            token=token,
            region=region,
            path=(
                f"/users/current/accounts/{account_id}/history-deals/"
                f"position/{encoded_position}"
            ),
        )
        if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
            raise MetaApiGatewayError("metaapi_invalid_response")
        return payload

    async def read_deals_by_time_range(
        self,
        *,
        token: str,
        account_id: str,
        region: str,
        start_time: datetime,
        end_time: datetime,
        offset: int = 0,
        limit: int = 1000,
    ) -> list[dict[str, object]]:
        """Read broker deal history for a time window, with MetaAPI REST pagination."""
        if offset < 0 or not 1 <= limit <= 1000:
            raise ValueError("Invalid MetaAPI history pagination.")
        start = quote(start_time.isoformat().replace("+00:00", "Z"), safe=":-T.Z+")
        end = quote(end_time.isoformat().replace("+00:00", "Z"), safe=":-T.Z+")
        payload = await self._read_terminal_json(
            token=token,
            region=region,
            path=(
                f"/users/current/accounts/{account_id}/history-deals/time/{start}/{end}"
                f"?offset={offset}&limit={limit}"
            ),
        )
        if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
            raise MetaApiGatewayError("metaapi_invalid_response")
        return payload

    async def read_symbol_price(
        self,
        *,
        token: str,
        account_id: str,
        region: str,
        symbol: str,
    ) -> dict[str, object]:
        encoded_symbol = quote(symbol, safe="")
        payload = await self._read_terminal_json(
            token=token,
            region=region,
            path=(
                f"/users/current/accounts/{account_id}/symbols/"
                f"{encoded_symbol}/current-price"
            ),
        )
        if not isinstance(payload, dict):
            raise MetaApiGatewayError("metaapi_invalid_response")
        return payload

    async def read_symbol_specification(
        self,
        *,
        token: str,
        account_id: str,
        region: str,
        symbol: str,
    ) -> dict[str, object]:
        encoded_symbol = quote(symbol, safe="")
        payload = await self._read_terminal_json(
            token=token,
            region=region,
            path=(
                f"/users/current/accounts/{account_id}/symbols/"
                f"{encoded_symbol}/specification"
            ),
        )
        if not isinstance(payload, dict):
            raise MetaApiGatewayError("metaapi_invalid_response")
        return payload

    async def _read_terminal_json(
        self, *, token: str, region: str, path: str
    ) -> object:
        normalized_region = normalize_metaapi_region(region)
        if normalized_region is None:
            raise MetaApiGatewayError("metaapi_region_unavailable")
        response = await self._request(
            "GET",
            f"https://mt-client-api-v1.{normalized_region}.agiliumtrade.ai{path}",
            token=token,
        )
        return self._json(response)

    async def _request(self, method: str, url: str, *, token: str) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.request(
                    method,
                    url,
                    headers={"Accept": "application/json", "auth-token": token},
                )
        except httpx.TimeoutException as exc:
            raise MetaApiGatewayError("metaapi_timeout", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise MetaApiGatewayError("metaapi_unreachable", retryable=True) from exc

        if response.status_code == 200:
            return response
        if response.status_code == 401:
            raise MetaApiGatewayError("metaapi_token_invalid")
        if response.status_code == 403:
            raise MetaApiGatewayError("metaapi_permission_denied")
        if response.status_code == 404:
            raise MetaApiGatewayError("metaapi_terminal_data_unavailable")
        if response.status_code in {408, 425, 429} or response.status_code >= 500:
            raise MetaApiGatewayError("metaapi_temporarily_unavailable", retryable=True)
        raise MetaApiGatewayError("metaapi_terminal_read_rejected")

    @staticmethod
    def _json(response: httpx.Response) -> object:
        try:
            return response.json()
        except ValueError as exc:
            raise MetaApiGatewayError("metaapi_invalid_response") from exc
