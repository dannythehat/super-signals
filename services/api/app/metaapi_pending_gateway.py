"""Canonical broker-side MetaAPI pending-order mutation.

Account eligibility/environment is decided by the shared execution service before this
gateway is called. This adapter therefore has no paper/LIVE policy of its own: the same
literal LIMIT/STOP request is sent to the already-selected MT5 account in both modes.
It never chases a pending level locally and never converts it to market.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_trade_gateway import MetaApiMarketOrderResult, MetaApiTradeGateway

_ACTIONS = {
    "buy_limit": "ORDER_TYPE_BUY_LIMIT",
    "sell_limit": "ORDER_TYPE_SELL_LIMIT",
    "buy_stop": "ORDER_TYPE_BUY_STOP",
    "sell_stop": "ORDER_TYPE_SELL_STOP",
}


@dataclass(frozen=True, slots=True)
class MetaApiPendingOrderRequest:
    order_type: str
    symbol: str
    volume: float
    open_price: float
    stop_loss: float
    take_profit: float | None
    client_id: str


class MetaApiPendingOrderGateway:
    def __init__(self, base: MetaApiTradeGateway | Any) -> None:
        self._base = base

    @staticmethod
    def _transport(base: Any) -> MetaApiTradeGateway:
        current = base
        seen: set[int] = set()
        while not isinstance(current, MetaApiTradeGateway):
            marker = id(current)
            if marker in seen:
                raise MetaApiGatewayError("pending_trade_gateway_invalid")
            seen.add(marker)
            current = getattr(current, "_base", None)
            if current is None:
                raise MetaApiGatewayError("pending_trade_gateway_invalid")
        return current

    async def place_pending_order(
        self,
        *,
        token: str,
        account_id: str,
        region: str,
        request: MetaApiPendingOrderRequest,
    ) -> MetaApiMarketOrderResult:
        action_type = _ACTIONS.get(request.order_type.strip().lower())
        if action_type is None:
            raise MetaApiGatewayError("pending_order_type_invalid")
        if request.open_price <= 0 or request.volume <= 0 or request.stop_loss <= 0:
            raise MetaApiGatewayError("trade_request_invalid")
        if request.take_profit is not None and request.take_profit <= 0:
            raise MetaApiGatewayError("trade_request_invalid")

        transport = self._transport(self._base)
        payload = await transport._trade_request(  # noqa: SLF001
            token=token,
            account_id=account_id,
            region=transport._normalize_region(region),  # noqa: SLF001
            json_body={
                "actionType": action_type,
                "symbol": request.symbol.strip().upper(),
                "volume": float(request.volume),
                "openPrice": float(request.open_price),
                "stopLoss": float(request.stop_loss),
                "stopLossUnits": "ABSOLUTE_PRICE",
                **(
                    {
                        "takeProfit": float(request.take_profit),
                        "takeProfitUnits": "ABSOLUTE_PRICE",
                    }
                    if request.take_profit is not None
                    else {}
                ),
                "clientId": request.client_id,
            },
        )
        order_id = str(payload.get("orderId") or "").strip()
        if not order_id:
            raise MetaApiGatewayError("metaapi_trade_result_missing_order")
        position_id = str(payload.get("positionId") or "").strip() or None
        return MetaApiMarketOrderResult(
            order_id=order_id,
            position_id=position_id,
            numeric_code=transport._numeric_code(payload),  # noqa: SLF001
            string_code=str(payload.get("stringCode") or "").strip()
            or "TRADE_RETCODE_PLACED",
        )


__all__ = ["MetaApiPendingOrderGateway", "MetaApiPendingOrderRequest"]
