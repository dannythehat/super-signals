"""Drop-in broker gateways backed by the outbound-polling Windows bridge."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.local_bridge_queue import LocalBridgeQueue
from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_margin_gateway import MetaApiMarginGateway
from app.metaapi_read_gateway import MetaApiReadGateway
from app.metaapi_trade_gateway import MetaApiTradeGateway


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


class LocalBridgeReadGateway(MetaApiReadGateway):
    """Expose local MT5 reads through the existing canonical gateway contract."""

    requires_metaapi_token = False

    def __init__(
        self,
        queue: LocalBridgeQueue,
        *,
        raw_balance_baseline: Decimal | str | float | None = None,
        canonical_balance_baseline: Decimal | str | float | None = None,
    ) -> None:
        self._queue = queue
        self._account_value_offset = self._balance_offset(
            raw_balance_baseline=raw_balance_baseline,
            canonical_balance_baseline=canonical_balance_baseline,
        )

    async def _execute(
        self,
        operation: str,
        *,
        account_id: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        return await self._queue.execute(
            account_id=account_id,
            operation=operation,
            payload=payload,
        )

    async def resolve_account_region(self, *, token: str, account_id: str) -> str:
        del token
        result = await self._execute("resolve_account_region", account_id=account_id)
        if not isinstance(result, dict) or not str(result.get("region") or "").strip():
            raise MetaApiGatewayError("metaapi_region_unavailable")
        return str(result["region"])

    async def read_account_information(
        self, *, token: str, account_id: str, region: str
    ) -> dict[str, object]:
        del token, region
        result = await self._execute("read_account_information", account_id=account_id)
        payload = self._dict(result)
        if self._account_value_offset is None:
            return payload

        adjusted = dict(payload)
        for key in ("balance", "equity", "freeMargin"):
            try:
                adjusted[key] = float(
                    Decimal(str(payload[key])) + self._account_value_offset
                )
            except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
                raise MetaApiGatewayError("metaapi_invalid_response") from exc

        try:
            margin = Decimal(str(payload.get("margin") or 0))
            adjusted_equity = Decimal(str(adjusted["equity"]))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise MetaApiGatewayError("metaapi_invalid_response") from exc
        adjusted["marginLevel"] = (
            float(adjusted_equity / margin * Decimal("100"))
            if margin > 0
            else payload.get("marginLevel")
        )
        return adjusted

    @staticmethod
    def _balance_offset(
        *,
        raw_balance_baseline: Decimal | str | float | None,
        canonical_balance_baseline: Decimal | str | float | None,
    ) -> Decimal | None:
        if raw_balance_baseline is None and canonical_balance_baseline is None:
            return None
        if raw_balance_baseline is None or canonical_balance_baseline is None:
            raise ValueError("local_bridge_balance_baselines_incomplete")
        try:
            return Decimal(str(canonical_balance_baseline)) - Decimal(
                str(raw_balance_baseline)
            )
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("local_bridge_balance_baselines_invalid") from exc

    async def read_positions(
        self, *, token: str, account_id: str, region: str
    ) -> list[dict[str, object]]:
        del token, region
        result = await self._execute("read_positions", account_id=account_id)
        return self._list(result)

    async def read_orders(
        self, *, token: str, account_id: str, region: str
    ) -> list[dict[str, object]]:
        del token, region
        result = await self._execute("read_orders", account_id=account_id)
        return self._list(result)

    async def read_history_orders_by_ticket(
        self,
        *,
        token: str,
        account_id: str,
        region: str,
        order_id: str,
    ) -> list[dict[str, object]]:
        del token, region
        result = await self._execute(
            "read_history_orders_by_ticket",
            account_id=account_id,
            payload={"order_id": order_id},
        )
        return self._list(result)

    async def read_history_orders_by_time_range(
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
        del token, region
        result = await self._execute(
            "read_history_orders_by_time_range",
            account_id=account_id,
            payload={
                "start_time": _iso(start_time),
                "end_time": _iso(end_time),
                "offset": offset,
                "limit": limit,
            },
        )
        return self._list(result)

    async def read_deals_by_position(
        self,
        *,
        token: str,
        account_id: str,
        region: str,
        position_id: str,
    ) -> list[dict[str, object]]:
        del token, region
        result = await self._execute(
            "read_deals_by_position",
            account_id=account_id,
            payload={"position_id": position_id},
        )
        return self._list(result)

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
        del token, region
        result = await self._execute(
            "read_deals_by_time_range",
            account_id=account_id,
            payload={
                "start_time": _iso(start_time),
                "end_time": _iso(end_time),
                "offset": offset,
                "limit": limit,
            },
        )
        return self._list(result)

    async def read_symbol_price(
        self,
        *,
        token: str,
        account_id: str,
        region: str,
        symbol: str,
    ) -> dict[str, object]:
        del token, region
        result = await self._execute(
            "read_symbol_price", account_id=account_id, payload={"symbol": symbol}
        )
        return self._dict(result)

    async def read_symbol_specification(
        self,
        *,
        token: str,
        account_id: str,
        region: str,
        symbol: str,
    ) -> dict[str, object]:
        del token, region
        result = await self._execute(
            "read_symbol_specification",
            account_id=account_id,
            payload={"symbol": symbol},
        )
        return self._dict(result)

    @staticmethod
    def _dict(value: Any) -> dict[str, object]:
        if not isinstance(value, dict):
            raise MetaApiGatewayError("metaapi_invalid_response")
        return value

    @staticmethod
    def _list(value: Any) -> list[dict[str, object]]:
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise MetaApiGatewayError("metaapi_invalid_response")
        return value


class LocalBridgeTradeGateway(MetaApiTradeGateway):
    """Send canonical trade mutations to MT5 without changing execution policy."""

    def __init__(
        self,
        queue: LocalBridgeQueue,
        *,
        read_gateway: LocalBridgeReadGateway,
    ) -> None:
        self._queue = queue
        self._read = read_gateway

    async def _trade_request(
        self,
        *,
        token: str,
        account_id: str,
        region: str,
        json_body: dict[str, object],
    ) -> dict[str, object]:
        del token, region
        client_id = str(json_body.get("clientId") or "").strip()
        action = str(json_body.get("actionType") or "").strip()
        idempotency_key = None
        if client_id and action.startswith("ORDER_TYPE_"):
            idempotency_key = f"mt5-order:{account_id}:{client_id}"
        result = await self._queue.execute(
            account_id=account_id,
            operation="trade",
            payload=dict(json_body),
            idempotency_key=idempotency_key,
        )
        payload = LocalBridgeReadGateway._dict(result)
        numeric_code = self._numeric_code(payload)
        string_code = str(payload.get("stringCode") or "").strip()
        if numeric_code not in {0, 10008, 10009, 10010, 10025} and string_code not in {
            "ERR_NO_ERROR",
            "TRADE_RETCODE_PLACED",
            "TRADE_RETCODE_DONE",
            "TRADE_RETCODE_DONE_PARTIAL",
            "TRADE_RETCODE_NO_CHANGES",
        }:
            raise MetaApiGatewayError("metaapi_trade_rejected")
        return payload


class LocalBridgeMarginGateway(MetaApiMarginGateway):
    def __init__(self, queue: LocalBridgeQueue) -> None:
        self._queue = queue

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
        del token, region
        result = await self._queue.execute(
            account_id=account_id,
            operation="calculate_margin",
            payload={
                "symbol": symbol,
                "side": side,
                "volume": volume,
                "open_price": open_price,
            },
        )
        if not isinstance(result, dict):
            raise MetaApiGatewayError("metaapi_invalid_response")
        try:
            return float(result["margin"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MetaApiGatewayError("metaapi_invalid_response") from exc


__all__ = [
    "LocalBridgeMarginGateway",
    "LocalBridgeReadGateway",
    "LocalBridgeTradeGateway",
]
