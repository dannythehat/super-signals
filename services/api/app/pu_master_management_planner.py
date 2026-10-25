"""No-order PU Prime provider management planner.

Provider actions are translated only for positively matched, already-owned
PU master position tickets. This file does not call MT5 or enqueue commands.
A separate durable dispatcher and broker reconciliation are still required.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


class PuManagementBlocked(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class PuMappedPosition:
    ticket: str
    tp_index: int
    client_id: str
    side: str
    symbol: str
    open_price: Decimal
    stop_loss: Decimal
    take_profit: Decimal | None
    status: str = "open"


@dataclass(frozen=True, slots=True)
class PuManagementInstruction:
    ticket: str
    action_type: str
    payload: dict[str, Any]
    idempotency_key: str


def _targets(target: str, mapped: tuple[PuMappedPosition, ...]) -> tuple[PuMappedPosition, ...]:
    normalized = target.strip().lower()
    active = tuple(p for p in mapped if p.status == "open")
    if normalized == "all":
        return active
    if normalized == "partial_tp1":
        normalized = "tp1"
    if normalized.startswith("tp") and normalized[2:].isdigit():
        index = int(normalized[2:])
        return tuple(p for p in active if p.tp_index == index)
    # Provider entry-layer targeting needs its own atomic mapping, not guesses.
    raise PuManagementBlocked("unsupported_provider_management_target")


def _is_protective(position: PuMappedPosition, proposed: Decimal) -> bool:
    if position.side == "BUY":
        return proposed >= position.stop_loss
    if position.side == "SELL":
        return proposed <= position.stop_loss
    raise PuManagementBlocked("invalid_position_side")


def plan_pu_management(
    *,
    event_id: str,
    actions: list[dict[str, Any]],
    mapped_positions: tuple[PuMappedPosition, ...],
    broker_positions: list[dict[str, Any]],
    expected_symbol: str = "XAUUSD.s",
) -> tuple[PuManagementInstruction, ...]:
    if not event_id or not actions:
        raise PuManagementBlocked("missing_management_event")
    broker_by_ticket = {str(item.get("id") or ""): item for item in broker_positions}
    if len(broker_by_ticket) != len(broker_positions):
        raise PuManagementBlocked("duplicate_or_missing_broker_position_ticket")
    if len({item.ticket for item in mapped_positions}) != len(mapped_positions):
        raise PuManagementBlocked("duplicate_position_mapping")
    if any(p.symbol != expected_symbol or not p.client_id for p in mapped_positions):
        raise PuManagementBlocked("unsafe_pu_position_mapping")

    instructions: list[PuManagementInstruction] = []
    for action_index, action in enumerate(actions):
        kind = str(action.get("type") or "").strip().lower()
        target = str(action.get("target") or "").strip()
        if kind not in {"close", "move_to_break_even", "edit_stop_loss"}:
            raise PuManagementBlocked("unverified_management_action")
        positions = _targets(target, mapped_positions)
        for position in positions:
            broker = broker_by_ticket.get(position.ticket)
            if broker is None:
                # Settled TP/SL can legitimately leave no live ticket.
                # Reconciliation must prove it closed before skipping.
                raise PuManagementBlocked("broker_position_reconciliation_required")
            if broker.get("symbol") != expected_symbol:
                raise PuManagementBlocked("broker_symbol_mismatch")
            if str(broker.get("clientId") or "") != position.client_id:
                raise PuManagementBlocked("broker_position_ownership_mismatch")
            if kind == "close":
                payload = {"actionType": "POSITION_CLOSE_ID", "positionId": position.ticket}
            else:
                if kind == "move_to_break_even":
                    new_sl = position.open_price
                else:
                    try:
                        new_sl = Decimal(str(action["value"]))
                    except (KeyError, ValueError, TypeError) as exc:
                        raise PuManagementBlocked("invalid_stop_loss_update") from exc
                if not new_sl.is_finite() or new_sl <= 0:
                    raise PuManagementBlocked("invalid_stop_loss_update")
                if not _is_protective(position, new_sl):
                    raise PuManagementBlocked("provider_would_widen_stop_loss")
                if new_sl == position.stop_loss:
                    continue
                payload = {
                    "actionType": "POSITION_MODIFY",
                    "positionId": position.ticket,
                    "stopLoss": float(new_sl),
                    # Preserve TP while adjusting SL. An omitted TP is reset
                    # to zero in the MT5 bridge.
                    "takeProfit": float(position.take_profit or 0),
                }
            instructions.append(
                PuManagementInstruction(
                    ticket=position.ticket,
                    action_type=kind,
                    payload=payload,
                    idempotency_key=(
                        f"pu:management:{event_id}:{action_index}:"
                        f"{position.ticket}:{kind}"
                    ),
                )
            )
    return tuple(instructions)
