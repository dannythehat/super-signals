"""Demo-only broker-side pending order adapter for paper testing.

This adapter is deliberately incapable of operating a live account. The caller must
pass the persisted account environment and only ``demo`` is accepted before any HTTP
mutation can be sent. It exists so paper testing can exercise real Vantage Demo / MT5
pending-order semantics without creating a live-money path.
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
class PaperPendingOrderRequest:
    order_type: str
    symbol: str
    volume: float
    open_price: float
    stop_loss: float
    take_profit: float | None
    client_id: str


class PaperPendingOrderGateway:
    def __init__(self, base: MetaApiTradeGateway | Any) -> None:
        self._base = base

    @staticmethod
    def _transport(base: Any) -> MetaApiTradeGateway:
        """Resolve the real MetaAPI transport through narrow guard wrappers.

        The Owner executor passes Day28ZoneGuardTradeGateway, which intentionally
        exposes market/close/modify/cancel methods but not the private transport used
        by this pending adapter. Walk only the explicit ``_base`` wrapper chain until
        the real MetaApiTradeGateway is reached. Fail closed for any unknown object.
        """
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
        account_environment: str,
        token: str,
        account_id: str,
        region: str,
        request: PaperPendingOrderRequest,
    ) -> MetaApiMarketOrderResult:
        if account_environment.strip().lower() != "demo":
            raise MetaApiGatewayError("paper_pending_demo_account_required")
        action_type = _ACTIONS.get(request.order_type.strip().lower())
        if action_type is None:
            raise MetaApiGatewayError("pending_order_type_invalid")
        if request.open_price <= 0 or request.volume <= 0:
            raise MetaApiGatewayError("trade_request_invalid")

        transport = self._transport(self._base)
        payload = await transport._trade_request(  # noqa: SLF001 - narrow demo adapter
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
            string_code=str(payload.get("stringCode") or "").strip() or "TRADE_RETCODE_PLACED",
        )


__all__ = ["PaperPendingOrderGateway", "PaperPendingOrderRequest"]
