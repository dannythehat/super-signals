"""Narrow MetaAPI trade gateway for Super Signals demo execution and management.

Day 26 submits market positions and uses close-by-position-id for failure compensation.
Day 27 adds only the broker mutations required by explicit provider follow-ups:
position close, position SL/TP modify and pending-order cancel. All mutations address a
known broker position/order ID; there is no symbol-wide close or discretionary action.
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
    """MetaAPI client exposing the project's deliberately narrow trade mutations."""

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
        take_profit: float | None,
        client_id: str,
    ) -> MetaApiMarketOrderResult:
        normalized_region = self._normalize_region(region)

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
        if not self._positive_finite(volume) or not self._positive_finite(stop_loss):
            raise MetaApiGatewayError("trade_request_invalid")
        if take_profit is not None and not self._positive_finite(take_profit):
            raise MetaApiGatewayError("trade_request_invalid")
        if (
            not client_id
            or len(client_id) > _MAX_CLIENT_ID_LENGTH
            or not _CLIENT_ID.fullmatch(client_id)
        ):
            raise MetaApiGatewayError("trade_client_id_invalid")

        json_body: dict[str, object] = {
            "actionType": action_type,
            "symbol": normalized_symbol,
            "volume": float(volume),
            "stopLoss": float(stop_loss),
            "stopLossUnits": "ABSOLUTE_PRICE",
            "clientId": client_id,
        }
        if take_profit is not None:
            json_body["takeProfit"] = float(take_profit)
            json_body["takeProfitUnits"] = "ABSOLUTE_PRICE"

        payload = await self._trade_request(
            token=token,
            account_id=account_id,
            region=normalized_region,
            json_body=json_body,
        )
        order_id = str(payload.get("orderId") or "").strip()
        if not order_id:
            raise MetaApiGatewayError("metaapi_trade_result_missing_order")
        position_id_raw = str(payload.get("positionId") or "").strip()

        return MetaApiMarketOrderResult(
            order_id=order_id,
            position_id=position_id_raw or None,
            numeric_code=self._numeric_code(payload),
            string_code=str(payload.get("stringCode") or "").strip()
            or "TRADE_RETCODE_DONE",
        )

    async def close_position(
        self,
        *,
        token: str,
        account_id: str,
        region: str,
        position_id: str,
    ) -> None:
        """Fully close one known broker position by its immutable broker ID."""
        normalized_region = self._normalize_region(region)
        normalized_position_id = position_id.strip()
        if not normalized_position_id:
            raise MetaApiGatewayError("broker_position_id_invalid")
        await self._trade_request(
            token=token,
            account_id=account_id,
            region=normalized_region,
            json_body={
                "actionType": "POSITION_CLOSE_ID",
                "positionId": normalized_position_id,
            },
        )

    async def modify_position(
        self,
        *,
        token: str,
        account_id: str,
        region: str,
        position_id: str,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> None:
        """Modify SL and/or TP on one known broker position."""
        normalized_region = self._normalize_region(region)
        normalized_position_id = position_id.strip()
        if not normalized_position_id:
            raise MetaApiGatewayError("broker_position_id_invalid")
        if stop_loss is None and take_profit is None:
            raise MetaApiGatewayError("trade_request_invalid")
        if stop_loss is not None and not self._positive_finite(stop_loss):
            raise MetaApiGatewayError("trade_request_invalid")
        if take_profit is not None and not self._positive_finite(take_profit):
            raise MetaApiGatewayError("trade_request_invalid")

        body: dict[str, object] = {
            "actionType": "POSITION_MODIFY",
            "positionId": normalized_position_id,
        }
        if stop_loss is not None:
            body["stopLoss"] = float(stop_loss)
            body["stopLossUnits"] = "ABSOLUTE_PRICE"
        if take_profit is not None:
            body["takeProfit"] = float(take_profit)
            body["takeProfitUnits"] = "ABSOLUTE_PRICE"

        await self._trade_request(
            token=token,
            account_id=account_id,
            region=normalized_region,
            json_body=body,
        )

    async def cancel_order(
        self,
        *,
        token: str,
        account_id: str,
        region: str,
        order_id: str,
    ) -> None:
        """Cancel one known active broker order by ID."""
        normalized_region = self._normalize_region(region)
        normalized_order_id = order_id.strip()
        if not normalized_order_id:
            raise MetaApiGatewayError("broker_order_id_invalid")
        await self._trade_request(
            token=token,
            account_id=account_id,
            region=normalized_region,
            json_body={
                "actionType": "ORDER_CANCEL",
                "orderId": normalized_order_id,
            },
        )

    async def _trade_request(
        self,
        *,
        token: str,
        account_id: str,
        region: str,
        json_body: dict[str, object],
    ) -> dict[str, object]:
        response = await self._request(
            "POST",
            (
                f"https://mt-client-api-v1.{region}.agiliumtrade.ai"
                f"/users/current/accounts/{account_id}/trade"
            ),
            token=token,
            json_body=json_body,
        )
        payload = self._json(response)
        if not isinstance(payload, dict):
            raise MetaApiGatewayError("metaapi_invalid_response")

        numeric_code = self._numeric_code(payload)
        string_code = str(payload.get("stringCode") or "").strip()
        if string_code != "TRADE_RETCODE_DONE" and numeric_code != 10009:
            raise MetaApiGatewayError("metaapi_trade_rejected")
        return payload

    @staticmethod
    def _numeric_code(payload: dict[str, object]) -> int | None:
        raw_numeric_code = payload.get("numericCode")
        if raw_numeric_code is None:
            return None
        if isinstance(raw_numeric_code, bool):
            raise MetaApiGatewayError("metaapi_invalid_response")
        try:
            return int(raw_numeric_code)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise MetaApiGatewayError("metaapi_invalid_response") from exc

    @staticmethod
    def _normalize_region(region: str) -> str:
        normalized_region = region.strip().lower()
        if not _REGION.fullmatch(normalized_region):
            raise MetaApiGatewayError("metaapi_region_unavailable")
        return normalized_region

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
