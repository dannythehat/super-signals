"""Canonical layer-aware provider management shared by paper and future LIVE.

This class owns the management corrections found during paper testing directly. No
runtime patch is required. Broker mutations remain exact mapped position/order IDs.
"""

from __future__ import annotations

from contextvars import ContextVar
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.metaapi_gateway import MetaApiGatewayError
from app.mt5_crypto import BrokerCredentialDecryptionError
from app.mt5_management_day27 import Day27ManagementError, Day27ManagementResult
from app.paper_critical_management import PaperCriticalManagementService, _LayerPosition

_PROVIDER_PRICE_TOLERANCE = Decimal("0.75")
_RISK_FREE_FILL_TOLERANCE = Decimal("1.00")
_RISK_FREE_PREFIX = "best_entry_risk_free_"
_MANAGEMENT_EVENT_CUTOFF: ContextVar[Any | None] = ContextVar(
    "super_signals_management_event_cutoff",
    default=None,
)
_PROFITABLE_BROKER_IDS: ContextVar[frozenset[str]] = ContextVar(
    "super_signals_profitable_broker_ids",
    default=frozenset(),
)


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _positive_decimal(value: object) -> Decimal | None:
    parsed = _decimal_or_none(value)
    return parsed if parsed is not None and parsed > 0 else None


def _broker_position_is_profitable(payload: dict[str, object]) -> bool:
    """Broker floating P/L is authority; price geometry is fallback only."""
    profit = _decimal_or_none(payload.get("profit"))
    if profit is not None:
        return profit > 0
    opened = _positive_decimal(payload.get("openPrice"))
    current = _positive_decimal(payload.get("currentPrice"))
    if opened is None or current is None:
        return False
    raw_type = str(payload.get("type") or "").upper()
    if raw_type in {"POSITION_TYPE_BUY", "BUY"}:
        return current > opened
    if raw_type in {"POSITION_TYPE_SELL", "SELL"}:
        return current < opened
    return False


class PaperCriticalManagementV2(PaperCriticalManagementService):
    """Canonical management target selection, event-time safety and add-entry handling."""

    async def execute_owner_demo_event(
        self,
        *,
        owner_user_id: UUID,
        lifecycle_event_id: UUID,
    ) -> Day27ManagementResult:
        event = self._load_event(lifecycle_event_id)
        if event is None:
            raise Day27ManagementError("day27_lifecycle_event_not_found")
        actions = self._actions(event)
        add_actions = [action for action in actions if str(action.get("type") or "") == "add_market"]
        if add_actions:
            if len(actions) != 1 or len(add_actions) != 1:
                raise Day27ManagementError("day27_add_market_compound_unsupported")
            existing = self._existing_success(owner_user_id, lifecycle_event_id)
            if existing is not None:
                return existing
            return await self._execute_add_market(
                owner_user_id=owner_user_id,
                lifecycle_event_id=lifecycle_event_id,
                event=event,
                action=add_actions[0],
            )

        # Replayed management may mutate only positions that existed when the provider
        # sent that event. A pending layer filled later is outside its scope.
        with self._session_factory() as session:
            cutoff = session.execute(
                text(
                    """
                    SELECT occurred_at
                    FROM signal_lifecycle_events
                    WHERE id=:event_id
                    LIMIT 1
                    """
                ),
                {"event_id": lifecycle_event_id},
            ).scalar_one_or_none()
        token = _MANAGEMENT_EVENT_CUTOFF.set(cutoff)
        try:
            return await super().execute_owner_demo_event(
                owner_user_id=owner_user_id,
                lifecycle_event_id=lifecycle_event_id,
            )
        finally:
            _MANAGEMENT_EVENT_CUTOFF.reset(token)

    async def _execute_add_market(
        self,
        *,
        owner_user_id: UUID,
        lifecycle_event_id: UUID,
        event: Any,
        action: dict[str, Any],
    ) -> Day27ManagementResult:
        """Duplicate the currently protected active tranches as one new market layer."""
        requested_side = str(action.get("value") or "").strip().upper()
        if requested_side not in {"BUY", "SELL"}:
            raise Day27ManagementError("day27_add_market_side_invalid")

        signal_id = UUID(str(event["signal_id"]))
        account = self._load_account(owner_user_id)
        if account is None:
            raise Day27ManagementError("mt5_account_not_configured")
        try:
            token = self._cipher.decrypt(account.token_ciphertext)
        except BrokerCredentialDecryptionError as exc:
            raise Day27ManagementError("broker_credential_decryption_failed") from exc
        try:
            region = await self._read.resolve_account_region(
                token=token,
                account_id=account.account_id,
            )
        except MetaApiGatewayError as exc:
            raise Day27ManagementError(exc.code, retryable=exc.retryable) from exc

        with self._session_factory() as session:
            signal = session.execute(
                text("SELECT symbol,side FROM signals WHERE id=:signal_id LIMIT 1"),
                {"signal_id": signal_id},
            ).mappings().first()
            rows = session.execute(
                text(
                    """
                    SELECT id,entry_index,tp_index,planned_risk_percent,volume,
                           stop_loss,take_profit,broker_position_id
                    FROM positions
                    WHERE signal_id=:signal_id
                      AND user_id=:user_id
                      AND status='open'
                      AND broker_position_id IS NOT NULL
                    ORDER BY entry_index,tp_index
                    """
                ),
                {"signal_id": signal_id, "user_id": owner_user_id},
            ).mappings().all()
            max_entry = int(
                session.execute(
                    text(
                        """
                        SELECT COALESCE(MAX(entry_index),0)
                        FROM positions
                        WHERE signal_id=:signal_id AND user_id=:user_id
                        """
                    ),
                    {"signal_id": signal_id, "user_id": owner_user_id},
                ).scalar_one()
            )
        if signal is None:
            raise Day27ManagementError("signal_not_found")
        symbol = str(signal["symbol"] or "").strip().upper()
        side = str(signal["side"] or "").strip().upper()
        if symbol != "XAUUSD" or side != requested_side:
            raise Day27ManagementError("day27_add_market_signal_mismatch")
        if not rows:
            raise Day27ManagementError("day27_add_market_no_open_positions")

        broker_positions = await self._broker_positions(
            token=token,
            account_id=account.account_id,
            region=region,
        )
        new_entry_index = max_entry + 1
        planned: list[dict[str, Any]] = []
        for row in rows:
            broker_id = str(row["broker_position_id"] or "")
            broker = broker_positions.get(broker_id)
            if broker is None:
                continue
            volume = _positive_decimal(row["volume"])
            planned_risk = _positive_decimal(row["planned_risk_percent"])
            stop_loss = _positive_decimal(broker.get("stopLoss"))
            broker_tp = _positive_decimal(broker.get("takeProfit"))
            local_tp = _positive_decimal(row["take_profit"])
            if volume is None or planned_risk is None or stop_loss is None:
                raise Day27ManagementError("day27_add_market_protection_invalid")
            if local_tp is not None and broker_tp is None:
                raise Day27ManagementError("day27_add_market_broker_tp_missing")
            tp_index = int(row["tp_index"])
            client_id = f"SSX_{signal_id.hex[:8]}_{new_entry_index}{tp_index}"
            planned.append(
                {
                    "tp_index": tp_index,
                    "planned_risk_percent": planned_risk,
                    "volume": volume,
                    "stop_loss": stop_loss,
                    "take_profit": broker_tp,
                    "client_id": client_id,
                }
            )
        if not planned:
            raise Day27ManagementError("day27_add_market_no_broker_positions")

        created: list[dict[str, Any]] = []
        try:
            for item in planned:
                current_positions = await self._broker_positions(
                    token=token,
                    account_id=account.account_id,
                    region=region,
                )
                already = next(
                    (
                        payload
                        for payload in current_positions.values()
                        if str(payload.get("clientId") or "") == item["client_id"]
                    ),
                    None,
                )
                if already is not None:
                    position_id = str(already.get("id") or "")
                    order_id = str(already.get("orderId") or position_id)
                else:
                    result = await self._trade.place_market_order(
                        token=token,
                        account_id=account.account_id,
                        region=region,
                        side=side,
                        symbol=symbol,
                        volume=float(item["volume"]),
                        stop_loss=float(item["stop_loss"]),
                        take_profit=(
                            float(item["take_profit"])
                            if item["take_profit"] is not None
                            else None
                        ),
                        client_id=item["client_id"],
                    )
                    order_id = result.order_id
                    position_id = str(result.position_id or "")
                    if not position_id:
                        refreshed = await self._broker_positions(
                            token=token,
                            account_id=account.account_id,
                            region=region,
                        )
                        matched = next(
                            (
                                payload
                                for payload in refreshed.values()
                                if str(payload.get("clientId") or "") == item["client_id"]
                            ),
                            None,
                        )
                        position_id = str((matched or {}).get("id") or "")
                if not position_id:
                    raise Day27ManagementError("day27_add_market_position_unresolved")
                created.append({**item, "order_id": order_id, "position_id": position_id})
        except (MetaApiGatewayError, Day27ManagementError) as exc:
            for item in reversed(created):
                try:
                    await self._trade.close_position(
                        token=token,
                        account_id=account.account_id,
                        region=region,
                        position_id=item["position_id"],
                    )
                except Exception:
                    pass
            if isinstance(exc, Day27ManagementError):
                raise
            raise Day27ManagementError(exc.code, retryable=exc.retryable) from exc

        refreshed = await self._broker_positions(
            token=token,
            account_id=account.account_id,
            region=region,
        )
        opened_at = datetime.now(UTC)
        with self._session_factory() as session:
            for item in created:
                broker = refreshed.get(item["position_id"], {})
                entry_price = _positive_decimal(broker.get("openPrice"))
                if entry_price is None:
                    raise Day27ManagementError("day27_add_market_entry_price_missing")
                session.execute(
                    text(
                        """
                        INSERT INTO positions (
                            signal_id,user_id,entry_index,tp_index,entry_order_type,
                            take_profit,planned_risk_percent,volume,stop_loss,
                            broker_order_id,broker_position_id,broker_client_id,
                            status,entry_price,opened_at
                        ) VALUES (
                            :signal_id,:user_id,:entry_index,:tp_index,'market',
                            :take_profit,:planned_risk_percent,:volume,:stop_loss,
                            :broker_order_id,:broker_position_id,:broker_client_id,
                            'open',:entry_price,:opened_at
                        )
                        ON CONFLICT (signal_id,user_id,entry_index,tp_index) DO NOTHING
                        """
                    ),
                    {
                        "signal_id": signal_id,
                        "user_id": owner_user_id,
                        "entry_index": new_entry_index,
                        "tp_index": item["tp_index"],
                        "take_profit": item["take_profit"],
                        "planned_risk_percent": item["planned_risk_percent"],
                        "volume": item["volume"],
                        "stop_loss": item["stop_loss"],
                        "broker_order_id": item["order_id"],
                        "broker_position_id": item["position_id"],
                        "broker_client_id": item["client_id"],
                        "entry_price": entry_price,
                        "opened_at": opened_at,
                    },
                )
            session.commit()

        result = Day27ManagementResult(
            lifecycle_event_id=lifecycle_event_id,
            signal_id=signal_id,
            user_id=owner_user_id,
            actions_requested=1,
            broker_actions_sent=len(created),
            positions_closed=0,
            positions_modified=0,
            orders_cancelled=0,
            external_positions_reconciled=0,
        )
        self._audit_success(result, (action,))
        return result

    async def _broker_positions(self, *, token: str, account_id: str, region: str):
        result = await super()._broker_positions(
            token=token,
            account_id=account_id,
            region=region,
        )
        _PROFITABLE_BROKER_IDS.set(
            frozenset(
                broker_id
                for broker_id, payload in result.items()
                if _broker_position_is_profitable(payload)
            )
        )
        return result

    def _load_layer_positions(
        self,
        signal_id: UUID,
        user_id: UUID,
    ) -> tuple[_LayerPosition, ...]:
        cutoff = _MANAGEMENT_EVENT_CUTOFF.get()
        if cutoff is None:
            return super()._load_layer_positions(signal_id, user_id)
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT id,entry_index,tp_index,broker_position_id,broker_order_id,
                           status,stop_loss,take_profit,entry_price,volume
                    FROM positions
                    WHERE signal_id=:signal_id
                      AND user_id=:user_id
                      AND COALESCE(opened_at,created_at)<=:cutoff
                    ORDER BY entry_index,tp_index
                    """
                ),
                {"signal_id": signal_id, "user_id": user_id, "cutoff": cutoff},
            ).mappings().all()
        return tuple(
            _LayerPosition(
                id=UUID(str(row["id"])),
                entry_index=int(row["entry_index"]),
                tp_index=int(row["tp_index"]),
                broker_position_id=(str(row["broker_position_id"]) if row["broker_position_id"] else None),
                broker_order_id=(str(row["broker_order_id"]) if row["broker_order_id"] else None),
                status=str(row["status"]),
                stop_loss=self._positive_decimal(row["stop_loss"]),
                take_profit=self._positive_decimal(row["take_profit"]),
                entry_price=self._positive_decimal(row["entry_price"]),
                volume=self._positive_decimal(row["volume"]),
            )
            for row in rows
        )

    @staticmethod
    def _needs_critical_management(actions: tuple[dict[str, Any], ...]) -> bool:
        tokens = (
            "entry_",
            "layer",
            "partial",
            "best_entry",
            "all_but_best",
            "entry_price_",
            "remaining",
            "profitable_only",
        )
        return any(
            any(token in str(action.get("target") or "").lower() for token in tokens)
            for action in actions
        )

    @classmethod
    def _select_layer_positions(
        cls,
        positions: tuple[_LayerPosition, ...],
        target: str,
        *,
        side: str,
    ) -> tuple[_LayerPosition, ...]:
        normalized = target.strip().lower()
        if normalized == "profitable_only":
            profitable_ids = _PROFITABLE_BROKER_IDS.get()
            return tuple(
                item
                for item in positions
                if item.broker_position_id is not None
                and item.broker_position_id in profitable_ids
            )
        if normalized.startswith(_RISK_FREE_PREFIX):
            wanted = cls._target_price(
                normalized.removeprefix(_RISK_FREE_PREFIX),
                "risk_free_stop_invalid",
            )
            groups, representative = cls._entry_groups(positions)
            if not groups:
                return ()
            best_index = cls._best_entry_index(representative, side=side)
            best_fill = representative[best_index]
            normalized_side = side.strip().upper()
            if normalized_side not in {"BUY", "SELL"}:
                raise Day27ManagementError("trade_side_invalid")
            protective = wanted >= best_fill if normalized_side == "BUY" else wanted <= best_fill
            if not protective and abs(wanted - best_fill) > _RISK_FREE_FILL_TOLERANCE:
                raise Day27ManagementError("risk_free_stop_not_protective")
            return tuple(groups[best_index])
        if normalized in {"best_entry", "all_but_best"}:
            groups, representative = cls._entry_groups(positions)
            if not groups:
                return ()
            best_index = cls._best_entry_index(representative, side=side)
            if normalized == "best_entry":
                return tuple(groups[best_index])
            return tuple(
                item
                for entry_index, items in groups.items()
                if entry_index != best_index
                for item in items
            )
        if normalized.startswith("entry_price_"):
            wanted = cls._target_price(
                normalized.removeprefix("entry_price_"),
                "layer_provider_price_invalid",
            )
            groups, representative = cls._entry_groups(positions)
            if not groups:
                return ()
            distances = {
                entry_index: abs(price - wanted)
                for entry_index, price in representative.items()
            }
            nearest_distance = min(distances.values())
            nearest = [
                entry_index
                for entry_index, distance in distances.items()
                if distance == nearest_distance
            ]
            if len(nearest) != 1 or nearest_distance > _PROVIDER_PRICE_TOLERANCE:
                raise Day27ManagementError("layer_provider_price_unresolved")
            return tuple(groups[nearest[0]])
        return super()._select_layer_positions(positions, target, side=side)

    @staticmethod
    def _best_entry_index(representative: dict[int, Decimal], *, side: str) -> int:
        normalized_side = side.strip().upper()
        if normalized_side == "BUY":
            return min(representative, key=representative.get)
        if normalized_side == "SELL":
            return max(representative, key=representative.get)
        raise Day27ManagementError("trade_side_invalid")

    @staticmethod
    def _target_price(raw: str, error_code: str) -> Decimal:
        try:
            wanted = Decimal(raw)
        except (InvalidOperation, ValueError):
            raise Day27ManagementError(error_code) from None
        if not wanted.is_finite() or wanted <= 0:
            raise Day27ManagementError(error_code)
        return wanted

    @staticmethod
    def _entry_groups(
        positions: tuple[_LayerPosition, ...],
    ) -> tuple[dict[int, list[_LayerPosition]], dict[int, Decimal]]:
        groups: dict[int, list[_LayerPosition]] = {}
        for item in positions:
            groups.setdefault(item.entry_index, []).append(item)
        representative: dict[int, Decimal] = {}
        for entry_index, items in groups.items():
            values = [item.entry_price for item in items if item.entry_price is not None]
            if not values:
                raise Day27ManagementError("layer_entry_price_unavailable")
            representative[entry_index] = sum(values, Decimal("0")) / Decimal(len(values))
        return groups, representative


__all__ = ["PaperCriticalManagementV2"]
