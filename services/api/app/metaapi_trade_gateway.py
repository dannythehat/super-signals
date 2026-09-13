"""Narrow MetaAPI trade gateway for Super Signals execution and management.

Every mutation targets a known broker position/order ID. Position modification is
protection-preserving: MetaAPI treats POSITION_MODIFY as replacement-like, so an
SL-only change must re-send the broker's current TP and a TP-only change must re-send
the broker's current SL. Broker state is read immediately before the mutation; if the
position cannot be resolved the mutation fails closed. A genuine runner may remain
without a TP, but an existing protected trade may never silently lose its SL.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

import httpx

from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_read_gateway import MetaApiReadGateway
from app.weekend_trading_freeze import weekend_trading_frozen

_REGION = re.compile(r"^[a-z0-9-]{2,64}$")
_CLIENT_ID = re.compile(r"^[A-Za-z0-9]+_[A-Za-z0-9]+_[A-Za-z0-9]+$")
_MAX_CLIENT_ID_LENGTH = 26

_SUCCESS_NUMERIC_CODES = {0, 10008, 10009, 10010, 10025}
_SUCCESS_STRING_CODES = {
    "ERR_NO_ERROR",
    "TRADE_RETCODE_PLACED",
    "TRADE_RETCODE_DONE",
    "TRADE_RETCODE_DONE_PARTIAL",
    "TRADE_RETCODE_NO_CHANGES",
}


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
        self._read = MetaApiReadGateway(timeout_seconds=timeout_seconds)

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
        """Modify SL/TP while preserving the untouched broker protection leg."""
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

        requested_stop_change = stop_loss is not None
        requested_tp_change = take_profit is not None
        if not (requested_stop_change and requested_tp_change):
            current_stop, current_tp = await self._current_protection(
                token=token,
                account_id=account_id,
                region=normalized_region,
                position_id=normalized_position_id,
            )
            if stop_loss is None:
                stop_loss = current_stop
            if take_profit is None:
                take_profit = current_tp
            if requested_tp_change and stop_loss is None:
                raise MetaApiGatewayError("broker_stop_loss_missing")

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

    async def _current_protection(
        self,
        *,
        token: str,
        account_id: str,
        region: str,
        position_id: str,
    ) -> tuple[float | None, float | None]:
        positions = await self._read.read_positions(
            token=token,
            account_id=account_id,
            region=region,
        )
        broker = next(
            (
                item
                for item in positions
                if str(item.get("id") or "").strip() == position_id
            ),
            None,
        )
        if broker is None:
            raise MetaApiGatewayError("broker_position_mapping_missing")
        return (
            self._positive_or_none(broker.get("stopLoss")),
            self._positive_or_none(broker.get("takeProfit")),
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
        if (
            numeric_code not in _SUCCESS_NUMERIC_CODES
            and string_code not in _SUCCESS_STRING_CODES
        ):
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
    def _positive_or_none(value: object) -> float | None:
        if isinstance(value, bool) or value is None:
            return None
        try:
            parsed = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) and parsed > 0 else None

    @classmethod
    def _positive_finite(cls, value: object) -> bool:
        return cls._positive_or_none(value) is not None

    @staticmethod
    def _json(response: httpx.Response) -> object:
        try:
            return response.json()
        except ValueError as exc:
            raise MetaApiGatewayError("metaapi_invalid_response") from exc
