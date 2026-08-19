"""Canonical layer-aware provider management shared by paper and future LIVE.

This class owns the management corrections found during paper testing directly. No
runtime patch is required. Broker mutations remain exact mapped position/order IDs.
"""

from __future__ import annotations

from contextvars import ContextVar
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.mt5_management_day27 import Day27ManagementError
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


def _broker_position_is_profitable(payload: dict[str, object]) -> bool:
    """Broker floating P/L is authority; price geometry is fallback only."""
    profit = _decimal_or_none(payload.get("profit"))
    if profit is not None:
        return profit > 0
    opened = _decimal_or_none(payload.get("openPrice"))
    current = _decimal_or_none(payload.get("currentPrice"))
    if opened is None or current is None:
        return False
    raw_type = str(payload.get("type") or "").upper()
    if raw_type in {"POSITION_TYPE_BUY", "BUY"}:
        return current > opened
    if raw_type in {"POSITION_TYPE_SELL", "SELL"}:
        return current < opened
    return False


class PaperCriticalManagementV2(PaperCriticalManagementService):
    """Canonical management target selection and event-time safety."""

    async def execute_owner_demo_event(
        self,
        *,
        owner_user_id: UUID,
        lifecycle_event_id: UUID,
    ):
        # A replayed provider event may mutate only positions that existed when the
        # provider sent that event. A pending layer filled later is outside its scope.
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
                    SELECT id, entry_index, tp_index, broker_position_id,
                           broker_order_id, status, stop_loss, take_profit,
                           entry_price, volume
                    FROM positions
                    WHERE signal_id=:signal_id
                      AND user_id=:user_id
                      AND COALESCE(opened_at, created_at) <= :cutoff
                    ORDER BY entry_index, tp_index
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
            # Provider price is authoritative across ordinary fill slippage, but a
            # materially losing stop contradicts "risk free" and fails closed.
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
    def _best_entry_index(
        representative: dict[int, Decimal],
        *,
        side: str,
    ) -> int:
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
