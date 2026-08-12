"""Read-only MetaAPI terminal data gateway for Day 23+ trading gates.

This module exposes account-state, position, open-order, quote and symbol-specification
reads only. There is no trade/order mutation method here.
"""

from __future__ import annotations

import re
from urllib.parse import quote

import httpx

from app.metaapi_gateway import MetaApiGatewayError

DEFAULT_METAAPI_PROVISIONING_URL = (
    "https://mt-provisioning-api-v1.agiliumtrade.agiliumtrade.ai"
)
_REGION = re.compile(r"^[a-z0-9-]{2,64}$")


class MetaApiReadGateway:
    """Small read-only client for MetaAPI terminal and market state."""

    def __init__(self, *, timeout_seconds: float = 30.0) -> None:
        self._timeout = httpx.Timeout(timeout_seconds)
        self._region_cache: dict[str, str] = {}

    async def resolve_account_region(self, *, token: str, account_id: str) -> str:
        cached = self._region_cache.get(account_id)
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
        region = str(payload.get("region") or "").strip().lower()
        if not _REGION.fullmatch(region):
            raise MetaApiGatewayError("metaapi_region_unavailable")
        self._region_cache[account_id] = region
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
        normalized_region = region.strip().lower()
        if not _REGION.fullmatch(normalized_region):
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
