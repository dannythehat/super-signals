"""Translate the bridge command contract to the official MetaTrader5 package."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


class LocalMt5Error(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def _time(value: int | float | None) -> str | None:
    if not value:
        return None
    return datetime.fromtimestamp(float(value), tz=UTC).isoformat().replace("+00:00", "Z")


class Mt5Adapter:
    SUCCESS_CODES = {10008, 10009, 10010, 10025}

    def __init__(
        self,
        *,
        terminal_path: str,
        login: int,
        password: str,
        server: str,
        magic: int,
        deviation: int = 20,
    ) -> None:
        try:
            import MetaTrader5 as mt5  # type: ignore[import-not-found]
        except ImportError as exc:
            raise LocalMt5Error("local_bridge_mt5_package_missing") from exc
        self.mt5 = mt5
        self.terminal_path = terminal_path
        self.login = login
        self.password = password
        self.server = server
        self.magic = magic
        self.deviation = deviation

    def connect(self) -> None:
        ok = self.mt5.initialize(
            path=self.terminal_path,
            login=self.login,
            password=self.password,
            server=self.server,
            timeout=30_000,
        )
        if not ok:
            raise LocalMt5Error("local_bridge_mt5_unavailable", str(self.mt5.last_error()))
        account = self.mt5.account_info()
        if account is None or int(account.login) != self.login:
            self.mt5.shutdown()
            raise LocalMt5Error("local_bridge_account_mismatch")

    def close(self) -> None:
        self.mt5.shutdown()

    def execute(self, operation: str, payload: dict[str, Any]) -> Any:
        handler = getattr(self, f"_op_{operation}", None)
        if handler is None:
            raise LocalMt5Error("local_bridge_operation_unsupported")
        if self.mt5.terminal_info() is None:
            self.connect()
        return handler(payload)

    def _op_resolve_account_region(self, _: dict[str, Any]) -> dict[str, str]:
        return {"region": "local"}

    def _op_read_account_information(self, _: dict[str, Any]) -> dict[str, Any]:
        item = self.mt5.account_info()
        if item is None:
            raise LocalMt5Error("local_bridge_mt5_unavailable")
        return {
            "currency": item.currency,
            "balance": item.balance,
            "credit": item.credit,
            "equity": item.equity,
            "margin": item.margin,
            "freeMargin": item.margin_free,
            "marginLevel": item.margin_level,
            "leverage": item.leverage,
            "tradeAllowed": bool(item.trade_allowed),
        }

    def _op_read_positions(self, _: dict[str, Any]) -> list[dict[str, Any]]:
        return [self._position(item) for item in (self.mt5.positions_get() or ())]

    def _op_read_orders(self, _: dict[str, Any]) -> list[dict[str, Any]]:
        return [self._order(item) for item in (self.mt5.orders_get() or ())]

    def _op_read_history_orders_by_ticket(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        rows = self.mt5.history_orders_get(ticket=int(payload["order_id"])) or ()
        return [self._order(item) for item in rows]

    def _op_read_history_orders_by_time_range(
        self, payload: dict[str, Any]
    ) -> list[dict[str, Any]]:
        rows = (
            self.mt5.history_orders_get(
                self._datetime(payload["start_time"]), self._datetime(payload["end_time"])
            )
            or ()
        )
        start = int(payload.get("offset", 0))
        limit = int(payload.get("limit", 1000))
        return [self._order(item) for item in rows[start : start + limit]]

    def _op_read_deals_by_position(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        rows = self.mt5.history_deals_get(position=int(payload["position_id"])) or ()
        return [self._deal(item) for item in rows]

    def _op_read_deals_by_time_range(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        rows = (
            self.mt5.history_deals_get(
                self._datetime(payload["start_time"]), self._datetime(payload["end_time"])
            )
            or ()
        )
        start = int(payload.get("offset", 0))
        limit = int(payload.get("limit", 1000))
        return [self._deal(item) for item in rows[start : start + limit]]

    def _op_read_symbol_price(self, payload: dict[str, Any]) -> dict[str, Any]:
        symbol = self._select_symbol(str(payload["symbol"]))
        tick = self.mt5.symbol_info_tick(symbol)
        info = self.mt5.symbol_info(symbol)
        if tick is None or info is None:
            raise LocalMt5Error("local_bridge_symbol_unavailable")
        return {
            "symbol": symbol,
            "bid": tick.bid,
            "ask": tick.ask,
            "time": _time(tick.time),
            "profitTickValue": info.trade_tick_value_profit,
            "lossTickValue": info.trade_tick_value_loss,
        }

    def _op_read_symbol_specification(self, payload: dict[str, Any]) -> dict[str, Any]:
        symbol = self._select_symbol(str(payload["symbol"]))
        info = self.mt5.symbol_info(symbol)
        if info is None:
            raise LocalMt5Error("local_bridge_symbol_unavailable")
        return {
            "symbol": symbol,
            "digits": info.digits,
            "point": info.point,
            "tickSize": info.trade_tick_size or info.point,
            "minVolume": info.volume_min,
            "maxVolume": info.volume_max,
            "volumeStep": info.volume_step,
            "contractSize": info.trade_contract_size,
        }

    def _op_calculate_margin(self, payload: dict[str, Any]) -> dict[str, float]:
        symbol = self._select_symbol(str(payload["symbol"]))
        side = str(payload["side"]).upper()
        if side not in {"BUY", "SELL"}:
            raise LocalMt5Error("trade_side_invalid")
        order_type = self.mt5.ORDER_TYPE_BUY if side == "BUY" else self.mt5.ORDER_TYPE_SELL
        margin = self.mt5.order_calc_margin(
            order_type,
            symbol,
            float(payload["volume"]),
            float(payload["open_price"]),
        )
        if margin is None:
            raise LocalMt5Error("local_bridge_margin_rejected", str(self.mt5.last_error()))
        return {"margin": float(margin)}

    def _op_trade(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = str(payload.get("actionType") or "")
        if action in {"POSITION_CLOSE_ID", "POSITION_PARTIAL"}:
            return self._close_position(
                int(payload["positionId"]),
                float(payload["volume"]) if action == "POSITION_PARTIAL" else None,
            )
        if action == "POSITION_MODIFY":
            request = {
                "action": self.mt5.TRADE_ACTION_SLTP,
                "position": int(payload["positionId"]),
                "sl": float(payload.get("stopLoss") or 0),
                "tp": float(payload.get("takeProfit") or 0),
            }
            return self._send(request)
        if action == "ORDER_CANCEL":
            return self._send(
                {"action": self.mt5.TRADE_ACTION_REMOVE, "order": int(payload["orderId"])}
            )

        type_map = {
            "ORDER_TYPE_BUY": self.mt5.ORDER_TYPE_BUY,
            "ORDER_TYPE_SELL": self.mt5.ORDER_TYPE_SELL,
            "ORDER_TYPE_BUY_LIMIT": self.mt5.ORDER_TYPE_BUY_LIMIT,
            "ORDER_TYPE_SELL_LIMIT": self.mt5.ORDER_TYPE_SELL_LIMIT,
            "ORDER_TYPE_BUY_STOP": self.mt5.ORDER_TYPE_BUY_STOP,
            "ORDER_TYPE_SELL_STOP": self.mt5.ORDER_TYPE_SELL_STOP,
        }
        if action not in type_map:
            raise LocalMt5Error("local_bridge_trade_action_invalid")
        symbol = self._select_symbol(str(payload["symbol"]))
        order_type = type_map[action]
        pending = action.endswith("_LIMIT") or action.endswith("_STOP")
        tick = self.mt5.symbol_info_tick(symbol)
        info = self.mt5.symbol_info(symbol)
        if tick is None or info is None:
            raise LocalMt5Error("local_bridge_symbol_unavailable")
        price = (
            float(payload["openPrice"])
            if pending
            else (tick.ask if order_type == self.mt5.ORDER_TYPE_BUY else tick.bid)
        )
        request = {
            "action": self.mt5.TRADE_ACTION_PENDING if pending else self.mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(payload["volume"]),
            "type": order_type,
            "price": price,
            "sl": float(payload.get("stopLoss") or 0),
            "tp": float(payload.get("takeProfit") or 0),
            "deviation": self.deviation,
            "magic": self.magic,
            "comment": str(payload.get("clientId") or "")[:31],
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": (
                self.mt5.ORDER_FILLING_RETURN if pending else self._market_filling_mode(info)
            ),
        }
        return self._send(request, client_id=str(payload.get("clientId") or ""))

    def _close_position(self, ticket: int, volume: float | None) -> dict[str, Any]:
        rows = self.mt5.positions_get(ticket=ticket) or ()
        if not rows:
            raise LocalMt5Error("broker_position_mapping_missing")
        position = rows[0]
        tick = self.mt5.symbol_info_tick(position.symbol)
        info = self.mt5.symbol_info(position.symbol)
        if tick is None or info is None:
            raise LocalMt5Error("local_bridge_symbol_unavailable")
        is_buy = position.type == self.mt5.POSITION_TYPE_BUY
        request = {
            "action": self.mt5.TRADE_ACTION_DEAL,
            "position": ticket,
            "symbol": position.symbol,
            "volume": float(volume if volume is not None else position.volume),
            "type": self.mt5.ORDER_TYPE_SELL if is_buy else self.mt5.ORDER_TYPE_BUY,
            "price": tick.bid if is_buy else tick.ask,
            "deviation": self.deviation,
            "magic": self.magic,
            "comment": "SS_CLOSE",
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": self._market_filling_mode(info),
        }
        return self._send(request)

    def _send(self, request: dict[str, Any], *, client_id: str = "") -> dict[str, Any]:
        if request.get("action") in {
            self.mt5.TRADE_ACTION_DEAL,
            self.mt5.TRADE_ACTION_PENDING,
        }:
            check = self.mt5.order_check(request)
            if check is None or int(check.retcode) not in {0, 10025}:
                message = (
                    getattr(check, "comment", "")
                    if check is not None
                    else str(self.mt5.last_error())
                )
                raise LocalMt5Error("local_bridge_trade_check_rejected", message)
        result = self.mt5.order_send(request)
        if result is None:
            raise LocalMt5Error("local_bridge_trade_result_missing", str(self.mt5.last_error()))
        code = int(result.retcode)
        if code not in self.SUCCESS_CODES:
            raise LocalMt5Error("local_bridge_trade_rejected", str(result.comment))
        position_id = None
        if client_id:
            matches = [
                item
                for item in (self.mt5.positions_get() or ())
                if str(getattr(item, "comment", "")) == client_id
            ]
            if matches:
                position_id = str(matches[-1].ticket)
        return {
            "orderId": str(result.order or result.deal or ""),
            "positionId": position_id,
            "numericCode": code,
            "stringCode": "TRADE_RETCODE_PLACED" if code == 10008 else "TRADE_RETCODE_DONE",
        }

    def _select_symbol(self, symbol: str) -> str:
        normalized = symbol.strip().upper()
        candidates = [normalized]
        if normalized == "XAUUSD":
            candidates.extend(("GOLD", "XAUUSD.", "XAUUSDm"))
        for candidate in candidates:
            info = self.mt5.symbol_info(candidate)
            if info is not None and (info.visible or self.mt5.symbol_select(candidate, True)):
                return candidate
        raise LocalMt5Error("local_bridge_symbol_unavailable")

    def _market_filling_mode(self, info: Any) -> int:
        # symbol_info.filling_mode is a SYMBOL_FILLING bit mask, while order_send
        # expects one ORDER_FILLING enum value. They are not numerically interchangeable.
        flags = int(info.filling_mode)
        if flags & 2:
            return self.mt5.ORDER_FILLING_IOC
        if flags & 1:
            return self.mt5.ORDER_FILLING_FOK
        return self.mt5.ORDER_FILLING_RETURN

    def _position(self, item: Any) -> dict[str, Any]:
        return {
            "id": str(item.ticket),
            "symbol": item.symbol,
            "type": "POSITION_TYPE_BUY"
            if item.type == self.mt5.POSITION_TYPE_BUY
            else "POSITION_TYPE_SELL",
            "volume": item.volume,
            "openPrice": item.price_open,
            "currentPrice": item.price_current,
            "stopLoss": item.sl or None,
            "takeProfit": item.tp or None,
            "profit": item.profit,
            "swap": item.swap,
            "commission": 0.0,
            "time": _time(item.time),
            "updateTime": _time(getattr(item, "time_update", item.time)),
            "clientId": item.comment or None,
        }

    def _order(self, item: Any) -> dict[str, Any]:
        type_names = {
            self.mt5.ORDER_TYPE_BUY_LIMIT: "ORDER_TYPE_BUY_LIMIT",
            self.mt5.ORDER_TYPE_SELL_LIMIT: "ORDER_TYPE_SELL_LIMIT",
            self.mt5.ORDER_TYPE_BUY_STOP: "ORDER_TYPE_BUY_STOP",
            self.mt5.ORDER_TYPE_SELL_STOP: "ORDER_TYPE_SELL_STOP",
            self.mt5.ORDER_TYPE_BUY: "ORDER_TYPE_BUY",
            self.mt5.ORDER_TYPE_SELL: "ORDER_TYPE_SELL",
        }
        return {
            "id": str(item.ticket),
            "symbol": item.symbol,
            "type": type_names.get(item.type, str(item.type)),
            "state": "ORDER_STATE_PLACED",
            "volume": item.volume_initial,
            "currentVolume": item.volume_current,
            "openPrice": item.price_open,
            "stopLoss": item.sl or None,
            "takeProfit": item.tp or None,
            "time": _time(item.time_setup),
            "clientId": item.comment or None,
        }

    def _deal(self, item: Any) -> dict[str, Any]:
        reason_names = {
            getattr(self.mt5, "DEAL_REASON_CLIENT", -100): "DEAL_REASON_CLIENT",
            getattr(self.mt5, "DEAL_REASON_MOBILE", -101): "DEAL_REASON_MOBILE",
            getattr(self.mt5, "DEAL_REASON_WEB", -102): "DEAL_REASON_WEB",
            getattr(self.mt5, "DEAL_REASON_EXPERT", -103): "DEAL_REASON_EXPERT",
            getattr(self.mt5, "DEAL_REASON_SL", -104): "DEAL_REASON_SL",
            getattr(self.mt5, "DEAL_REASON_TP", -105): "DEAL_REASON_TP",
            getattr(self.mt5, "DEAL_REASON_SO", -106): "DEAL_REASON_SO",
        }
        entry_names = {
            self.mt5.DEAL_ENTRY_IN: "DEAL_ENTRY_IN",
            self.mt5.DEAL_ENTRY_OUT: "DEAL_ENTRY_OUT",
            self.mt5.DEAL_ENTRY_INOUT: "DEAL_ENTRY_INOUT",
            self.mt5.DEAL_ENTRY_OUT_BY: "DEAL_ENTRY_OUT_BY",
        }
        type_names = {
            self.mt5.DEAL_TYPE_BUY: "DEAL_TYPE_BUY",
            self.mt5.DEAL_TYPE_SELL: "DEAL_TYPE_SELL",
            self.mt5.DEAL_TYPE_BALANCE: "DEAL_TYPE_BALANCE",
            self.mt5.DEAL_TYPE_CREDIT: "DEAL_TYPE_CREDIT",
        }
        return {
            "id": str(item.ticket),
            "orderId": str(item.order),
            "positionId": str(item.position_id),
            "symbol": item.symbol,
            "type": type_names.get(item.type, str(item.type)),
            "entryType": entry_names.get(item.entry, str(item.entry)),
            "volume": item.volume,
            "price": item.price,
            "profit": item.profit,
            "commission": item.commission,
            "swap": item.swap,
            "time": _time(item.time),
            "clientId": item.comment or None,
            "reason": reason_names.get(item.reason, str(item.reason)),
        }

    @staticmethod
    def _datetime(value: Any) -> datetime:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


__all__ = ["LocalMt5Error", "Mt5Adapter"]
