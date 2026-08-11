"""Narrow MetaAPI market-order gateway for Day 26 demo execution.

Day 26 needs one capability only: submit a broker market order carrying the
provider's SL/TP and a Super Signals clientId. Follow-up modification/close
commands belong to Day 27 and are intentionally absent here.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

import httpx

from app.metaapi_gateway import MetaApiGatewayError

_REGION = re.compile(r"^[a-z0-9-]{2,64}$")
_CLIENT_ID = re.compile(r"^[A-Za-z0-9]+_[A-Za-z0-9]+_[A-Za-z0-9]+$")
_MAX_CLIENT_ID_LENGTH = 26


@dataclass(frozen=True, slots=True)
class MetaApiMarketOrderResult:
    order_id: str
    position_id: str | None
    numeric_code: int | None
    string_code: str


class MetaApiTradeGateway:
    """MetaAPI client exposing market entry only for the Day 26 demo gate."""

    def __init__(self, *, timeout_seconds: float = 30.0) -> None:
        self._timeout = httpx.Timeout(timeout_seconds)

    async def place_market_order(
        self,
        *,
        token: str,
        account_id: str,
        region: str,
        side: str,
        symbol: str,
        volume: float,
        stop_loss: float,
        take_profit: float,
        client_id: str,
    ) -> MetaApiMarketOrderResult:
        normalized_region = region.strip().lower()
        if not _REGION.fullmatch(normalized_region):
            raise MetaApiGatewayError("metaapi_region_unavailable")

        normalized_side = side.strip().upper()
        if normalized_side == "BUY":
            action_type = "ORDER_TYPE_BUY"
        elif normalized_side == "SELL":
            action_type = "ORDER_TYPE_SELL"
        else:
            raise MetaApiGatewayError("trade_side_invalid")

        normalized_symbol = symbol.strip().upper()
        if not normalized_symbol:
            raise MetaApiGatewayError("symbol_invalid")
        if not all(
            self._positive_finite(value)
            for value in (volume, stop_loss, take_profit)
        ):
            raise MetaApiGatewayError("trade_request_invalid")
        if (
            not client_id
            or len(client_id) > _MAX_CLIENT_ID_LENGTH
            or not _CLIENT_ID.fullmatch(client_id)
        ):
            raise MetaApiGatewayError("trade_client_id_invalid")

        response = await self._request(
            "POST",
            (
                f"https://mt-client-api-v1.{normalized_region}.agiliumtrade.ai"
                f"/users/current/accounts/{account_id}/trade"
            ),
            token=token,
            json_body={
                "actionType": action_type,
                "symbol": normalized_symbol,
                "volume": float(volume),
                "stopLoss": float(stop_loss),
                "takeProfit": float(take_profit),
                "stopLossUnits": "ABSOLUTE_PRICE",
                "takeProfitUnits": "ABSOLUTE_PRICE",
                "clientId": client_id,
            },
        )
        payload = self._json(response)
        if not isinstance(payload, dict):
            raise MetaApiGatewayError("metaapi_invalid_response")

        raw_numeric_code = payload.get("numericCode")
        numeric_code: int | None
        if raw_numeric_code is None:
            numeric_code = None
        elif isinstance(raw_numeric_code, bool):
            raise MetaApiGatewayError("metaapi_invalid_response")
        else:
            try:
                numeric_code = int(raw_numeric_code)  # type: ignore[arg-type]
            except (TypeError, ValueError) as exc:
                raise MetaApiGatewayError("metaapi_invalid_response") from exc

        string_code = str(payload.get("stringCode") or "").strip()
        if string_code != "TRADE_RETCODE_DONE" and numeric_code != 10009:
            raise MetaApiGatewayError("metaapi_trade_rejected")

        order_id = str(payload.get("orderId") or "").strip()
        if not order_id:
            raise MetaApiGatewayError("metaapi_trade_result_missing_order")
        position_id_raw = str(payload.get("positionId") or "").strip()

        return MetaApiMarketOrderResult(
            order_id=order_id,
            position_id=position_id_raw or None,
            numeric_code=numeric_code,
            string_code=string_code or "TRADE_RETCODE_DONE",
        )

    async def _request(
        self,
        method: str,
        url: str,
        *,
        token: str,
        json_body: dict[str, object],
    ) -> httpx.Response:
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
            raise MetaApiGatewayError("metaapi_trade_request_rejected")
        if response.status_code == 401:
            raise MetaApiGatewayError("metaapi_token_invalid")
        if response.status_code == 403:
            raise MetaApiGatewayError("metaapi_permission_denied")
        if response.status_code == 404:
            raise MetaApiGatewayError("metaapi_terminal_data_unavailable")
        if response.status_code in {408, 425, 429} or response.status_code >= 500:
            raise MetaApiGatewayError("metaapi_temporarily_unavailable", retryable=True)
        raise MetaApiGatewayError("metaapi_trade_rejected")

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
