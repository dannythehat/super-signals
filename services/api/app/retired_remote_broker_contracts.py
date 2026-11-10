"""Fail-closed compatibility contracts for retired remote-broker interfaces.

The owner has removed the third-party broker API. This module deliberately
contains NO network transports, credentials, token storage, trade calls,
fallback trading, or external URLs. It only keeps legacy imports loadable
while historical routes are retired and the Contabo MT5 bridge is authoritative.
"""
from __future__ import annotations

import re
import math
from dataclasses import dataclass
from uuid import uuid4

# Intentionally not a URL: the former provider provisioning endpoint is retired.
DEFAULT_METAAPI_PROVISIONING_URL = ""
# Historical MT5 magic identifier, retained solely for backwards compatibility.
SUPER_SIGNALS_MAGIC = 220022


class MetaApiGatewayError(RuntimeError):
    def __init__(self, code: str = "retired_broker_api_disabled", *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class MetaApiAccountState:
    account_id: str
    login: str
    server: str
    state: str
    connection_status: str


@dataclass(frozen=True, slots=True)
class MetaApiCreateResult:
    pending: bool
    account_id: str | None = None
    state: str | None = None
    retry_after_seconds: int = 3


@dataclass(frozen=True, slots=True)
class MetaApiMarketOrderResult:
    order_id: str
    position_id: str | None
    numeric_code: int | None
    string_code: str


@dataclass(frozen=True, slots=True)
class MetaApiPendingOrderRequest:
    order_type: str
    symbol: str
    volume: float
    open_price: float
    stop_loss: float
    take_profit: float | None
    client_id: str


@dataclass(frozen=True, slots=True)
class MetaApiTokenScope:
    jwt_payload_decoded: bool
    access_rule_ids: tuple[str, ...]
    applications: tuple[str, ...]
    roles: tuple[str, ...]
    resource_rules_present: bool

    @property
    def is_explicitly_narrowed(self) -> bool:
        return self.resource_rules_present or bool(self.access_rule_ids or self.applications)

    @property
    def has_terminal_access(self) -> bool:
        return False


def inspect_metaapi_token_scope(token: str) -> MetaApiTokenScope:
    # The retired third-party API is never a valid connection option.
    return MetaApiTokenScope(False, (), (), (), False)


def normalize_metaapi_region(region: str) -> str | None:
    if not isinstance(region, str):
        return None
    value = region.strip().lower()
    return value if re.fullmatch(r"[a-z0-9-]{2,64}", value) else None


def remember_metaapi_region(account_id: str, region: str) -> None:
    # No legacy region or remote-account state may be reintroduced.
    return None


def get_metaapi_region(account_id: str) -> None:
    return None


class _RetiredRemoteBroker:
    """Never makes a call to any remote third-party broker service."""
    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def __getattr__(self, name: str):
        async def _blocked(*args: object, **kwargs: object):
            raise MetaApiGatewayError()
        if name.startswith("_") and name not in {"_request","_read_terminal_json","_trade_request","_current_protection"}:
            raise AttributeError(name)
        return _blocked

    @staticmethod
    async def _request(*args: object, **kwargs: object):
        raise MetaApiGatewayError()


class MetaApiReadGateway(_RetiredRemoteBroker):
    pass


class MetaApiTradeGateway(_RetiredRemoteBroker):
    # Pure broker command geometry retained for the Contabo LocalBridge subclass.
    # No HTTP implementation exists in this class. Only LocalBridge overrides
    # _trade_request to push approved commands into its own MT5 queue.
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

    async def _trade_request(self, *, token: str, account_id: str, region: str, json_body: dict[str, object]) -> dict[str, object]:
        raise MetaApiGatewayError("retired_broker_api_disabled")

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



class MetaApiMarginGateway(_RetiredRemoteBroker):
    pass


class MetaApiProvisioningGateway(_RetiredRemoteBroker):
    @staticmethod
    def new_transaction_id() -> str:
        return str(uuid4())


class MetaApiPendingOrderGateway(_RetiredRemoteBroker):
    def __init__(self, base: object) -> None:
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



__all__ = [
    "MetaApiGatewayError", "MetaApiAccountState", "MetaApiCreateResult",
    "MetaApiMarketOrderResult", "MetaApiPendingOrderRequest", "MetaApiTokenScope",
    "inspect_metaapi_token_scope", "normalize_metaapi_region",
    "remember_metaapi_region", "get_metaapi_region", "MetaApiReadGateway",
    "MetaApiTradeGateway", "MetaApiMarginGateway", "MetaApiProvisioningGateway",
    "MetaApiPendingOrderGateway",
]
