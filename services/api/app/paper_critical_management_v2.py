"""TDC-aware layer target selection on top of the critical paper manager.

This stays deliberately narrow: only mechanically extracted targets produced by
``critical_entry_policy`` are added here. Broker mutations remain exact-ID operations
implemented by :class:`PaperCriticalManagementService`.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from app.mt5_management_day27 import Day27ManagementError
from app.paper_critical_management import PaperCriticalManagementService, _LayerPosition

_PROVIDER_PRICE_TOLERANCE = Decimal("0.75")
_RISK_FREE_PREFIX = "best_entry_risk_free_"


class PaperCriticalManagementV2(PaperCriticalManagementService):
    """Add best-layer and provider-price targeting without widening broker authority."""

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

            # A provider calling a stop "risk free" is not enough to make it so for
            # our actual broker fill. For a BUY, a protective stop must be at or above
            # the surviving layer's actual entry. For a SELL it must be at or below.
            # If the better provider layer never filled, this guard therefore refuses
            # to move a worse fill into a guaranteed loss, which is exactly the failure
            # mode observed in paper testing.
            protective = (
                wanted >= best_fill
                if normalized_side == "BUY"
                else wanted <= best_fill
                if normalized_side == "SELL"
                else False
            )
            if normalized_side not in {"BUY", "SELL"}:
                raise Day27ManagementError("trade_side_invalid")
            if not protective:
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
