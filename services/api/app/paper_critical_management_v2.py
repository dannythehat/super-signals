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
        if normalized in {"best_entry", "all_but_best"}:
            groups, representative = cls._entry_groups(positions)
            if not groups:
                return ()
            normalized_side = side.strip().upper()
            if normalized_side == "BUY":
                best_index = min(representative, key=representative.get)
            elif normalized_side == "SELL":
                best_index = max(representative, key=representative.get)
            else:
                raise Day27ManagementError("trade_side_invalid")
            if normalized == "best_entry":
                return tuple(groups[best_index])
            return tuple(
                item
                for entry_index, items in groups.items()
                if entry_index != best_index
                for item in items
            )

        if normalized.startswith("entry_price_"):
            raw = normalized.removeprefix("entry_price_")
            try:
                wanted = Decimal(raw)
            except (InvalidOperation, ValueError):
                raise Day27ManagementError("layer_provider_price_invalid") from None
            if not wanted.is_finite() or wanted <= 0:
                raise Day27ManagementError("layer_provider_price_invalid")
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
