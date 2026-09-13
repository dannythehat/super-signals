"""MetaAPI margin-calculation gateway for Day 25 preflight checks.

This gateway can ask the broker how much margin a proposed aggregate order would
require. It exposes no trade, order-placement, modification, or close method.
"""

from __future__ import annotations

import math
import re

import httpx

from app.metaapi_gateway import MetaApiGatewayError
from app.weekend_trading_freeze import weekend_trading_frozen

_REGION = re.compile(r"^[a-z0-9-]{2,64}$")


class MetaApiMarginGateway:
    """Non-trading MetaAPI client used only for broker margin calculation."""

    def __init__(self, *, timeout_seconds: float = 30.0) -> None:
        self._timeout = httpx.Timeout(timeout_seconds)

    async def calculate_margin(
        self,
        *,
        token: str,
        account_id: str,
        region: str,
        symbol: str,
        side: str,
        volume: float,
        open_price: float,
    ) -> float:
        normalized_region = region.strip().lower()
        if not _REGION.fullmatch(normalized_region):
            raise MetaApiGatewayError("metaapi_region_unavailable")

        normalized_side = side.strip().upper()
        if normalized_side == "BUY":
            order_type = "ORDER_TYPE_BUY"
        elif normalized_side == "SELL":
            order_type = "ORDER_TYPE_SELL"
        else:
            raise MetaApiGatewayError("trade_side_invalid")

        normalized_symbol = symbol.strip().upper()
        if not normalized_symbol:
            raise MetaApiGatewayError("symbol_invalid")
        if not self._positive_finite(volume) or not self._positive_finite(open_price):
            raise MetaApiGatewayError("margin_request_invalid")

        response = await self._request(
            "POST",
            (
                f"https://mt-client-api-v1.{normalized_region}.agiliumtrade.ai"
                f"/users/current/accounts/{account_id}/calculate-margin"
            ),
            token=token,
            json_body={
                "symbol": normalized_symbol,
                "type": order_type,
                "volume": float(volume),
                "openPrice": float(open_price),
            },
        )
        payload = self._json(response)
        if not isinstance(payload, dict):
            raise MetaApiGatewayError("metaapi_invalid_response")
        margin = payload.get("margin")
        if isinstance(margin, bool):
            raise MetaApiGatewayError("metaapi_invalid_response")
        try:
            parsed = float(margin)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise MetaApiGatewayError("metaapi_invalid_response") from exc
        if not math.isfinite(parsed) or parsed < 0:
            raise MetaApiGatewayError("metaapi_invalid_response")
        return parsed

    async def _request(
        self,
        method: str,
        url: str,
        *,
        token: str,
        json_body: dict[str, object],
    ) -> httpx.Response:
        if weekend_trading_frozen():
            raise MetaApiGatewayError("metaapi_weekend_frozen")
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.request(
                    method,
                    url,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "auth-token": token,
                    },
                    json=json_body,
                )
        except httpx.TimeoutException as exc:
            raise MetaApiGatewayError("metaapi_timeout", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise MetaApiGatewayError("metaapi_unreachable", retryable=True) from exc

        if response.status_code == 200:
            return response
        if response.status_code == 400:
            raise MetaApiGatewayError("metaapi_margin_request_rejected")
        if response.status_code == 401:
            raise MetaApiGatewayError("metaapi_token_invalid")
        if response.status_code == 403:
            raise MetaApiGatewayError("metaapi_permission_denied")
        if response.status_code == 404:
            raise MetaApiGatewayError("metaapi_terminal_data_unavailable")
        if response.status_code in {408, 425, 429} or response.status_code >= 500:
            raise MetaApiGatewayError("metaapi_temporarily_unavailable", retryable=True)
        raise MetaApiGatewayError("metaapi_margin_check_rejected")

    @staticmethod
    def _positive_finite(value: object) -> bool:
        if isinstance(value, bool):
            return False
        try:
            parsed = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        return math.isfinite(parsed) and parsed > 0

    @staticmethod
    def _json(response: httpx.Response) -> object:
        try:
            return response.json()
        except ValueError as exc:
            raise MetaApiGatewayError("metaapi_invalid_response") from exc
