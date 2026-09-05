from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.aidy_market_client import AIDY_QUOTE_MODE, AidyM1Bar, AidyM1Window, AidyMarketClient
from app.shadow_trading_v2 import _benchmark_pnl_usd, _leg_r

logger = logging.getLogger(__name__)
_SUPPORTED_STYLES = {"intraday", "swing_or_sparse"}
_MAX_WINDOW = timedelta(hours=48)
_TP_RE = re.compile(r"tp\s*(\d+)", re.IGNORECASE)
_ENTRY_RE = re.compile(r"entry[_\s]*(\d+)", re.IGNORECASE)
_ENTRY_PRICE_RE = re.compile(r"entry_price_([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
_FIRST_N_RE = re.compile(r"first_(\d+)_layers?", re.IGNORECASE)
_WORST_N_RE = re.compile(r"worst_(\d+)_layers?", re.IGNORECASE)


class ConcurrentResolution(RuntimeError):
    pass


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("aidy_replay_timestamp_timezone_required")
    return value.astimezone(UTC)


def _minute_floor(value: datetime) -> datetime:
    return _utc(value).replace(second=0, microsecond=0)


def _decimal(value: object, *, allow_none: bool = False) -> Decimal | None:
    if value is None and allow_none:
        return None
    if value is None or isinstance(value, bool):
        raise ValueError("aidy_replay_decimal_required")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("aidy_replay_decimal_invalid") from exc
    if not result.is_finite():
        raise ValueError("aidy_replay_decimal_invalid")
    return result


def _price(value: object) -> Decimal:
    result = _decimal(value)
    assert result is not None
    if result <= 0:
        raise ValueError("aidy_replay_price_invalid")
    return result


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def lifecycle_watermark(events: Iterable[dict[str, Any]]) -> str:
    ordered = sorted(
        (
            {
                "id": str(item["id"]),
                "created_at": _utc(item["created_at"]).isoformat(),
                "occurred_at": _utc(item["occurred_at"]).isoformat(),
                "event_key": str(item.get("event_key") or ""),
            }
            for item in events
        ),
        key=lambda item: (item["created_at"], item["id"]),
    )
    return hashlib.sha256(_canonical(ordered).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class OriginalGeometry:
    side: str
    order_type: str
    entry_low: Decimal
    entry_high: Decimal
    initial_stop: Decimal
    entry_index: int
    targets: dict[int, Decimal | None]
    runners: frozenset[int]

    @classmethod
    def from_payload(cls, value: object) -> "OriginalGeometry":
        if not isinstance(value, dict):
            raise ValueError("aidy_original_geometry_missing")
        side = str(value.get("side") or "").upper()
        order_type = str(value.get("entry_order_type") or "").lower()
        if side not in {"BUY", "SELL"}:
            raise ValueError("aidy_original_geometry_side_invalid")
        entry_low = _price(value.get("entry_low"))
        entry_high = _price(value.get("entry_high"))
        stop = _price(value.get("initial_stop"))
        entry_index = int(value.get("entry_index") or 0)
        if entry_index < 1:
            raise ValueError("aidy_original_geometry_entry_index_invalid")
        raw_legs = value.get("legs")
        if not isinstance(raw_legs, list) or not raw_legs:
            raise ValueError("aidy_original_geometry_legs_missing")
        targets: dict[int, Decimal | None] = {}
        runners: set[int] = set()
        for item in raw_legs:
            if not isinstance(item, dict):
                raise ValueError("aidy_original_geometry_leg_invalid")
            index = int(item.get("tp_index") or 0)
            if index < 1 or index in targets:
                raise ValueError("aidy_original_geometry_leg_invalid")
            runner = bool(item.get("is_runner"))
            target = None if runner else _price(item.get("target_price"))
            targets[index] = target
            if runner:
                runners.add(index)
        if side == "BUY" and stop >= max(entry_low, entry_high):
            raise ValueError("aidy_original_geometry_stop_invalid")
        if side == "SELL" and stop <= min(entry_low, entry_high):
            raise ValueError("aidy_original_geometry_stop_invalid")
        return cls(
            side=side,
            order_type=order_type,
            entry_low=min(entry_low, entry_high),
            entry_high=max(entry_low, entry_high),
            initial_stop=stop,
            entry_index=entry_index,
            targets=targets,
            runners=frozenset(runners),
        )


@dataclass(slots=True)
class LegState:
    id: UUID
    tp_index: int
    original_target: Decimal | None
    effective_target: Decimal | None
    is_runner: bool
    status: str = "pending"
    remaining_fraction: Decimal = Decimal("1")
    realized_r: Decimal = Decimal("0")
    exit_reason: str | None = None
    exit_price: Decimal | None = None
    opened_at: datetime | None = None
    closed_at: datetime | None = None


@dataclass(slots=True)
class ResolutionState:
    status: str
    entry_price: Decimal | None
    opened_at: datetime | None
    effective_stop: Decimal
    legs: list[LegState]
    market_cursor: datetime | None
    lifecycle_watermark: str | None
    score_block_reason: str | None = None
    note: str | None = None
    close_reason: str | None = None
    closed_at: datetime | None = None
    last_price: Decimal | None = None
    max_price: Decimal | None = None
    min_price: Decimal | None = None
    terminal: bool = False
    evidence_digest: str | None = None
    evidence_ledger: list[dict[str, Any]] = field(default_factory=list)

    @property
    def score_eligible(self) -> bool:
        return self.status == "closed" and self.terminal and self.score_block_reason is None

    @property
    def exclusion_reason(self) -> str | None:
        if self.score_block_reason:
            return self.score_block_reason
        if self.score_eligible:
            return None
        if self.status == "cancelled":
            return self.close_reason or "provider_cancelled_before_entry"
        if self.status == "missed":
            return self.close_reason or "market_entry_not_observed"
        return "outcome_pending_aidy_m1"


def _evidence_payload(bar: AidyM1Bar) -> dict[str, Any]:
    return {
        "open_time_utc": bar.open_time_utc.isoformat(),
        "revision_index": bar.revision_index,
        "first_observed_at": bar.first_observed_at.isoformat(),
        "payload_digest": bar.payload_digest,
        "open": str(bar.open),
        "high": str(bar.high),
        "low": str(bar.low),
        "close": str(bar.close),
    }


def _chain_bar(state: ResolutionState, bar: AidyM1Bar) -> None:
    prior = state.evidence_digest or ""
    state.evidence_digest = hashlib.sha256(
        (prior + "|" + _canonical(_evidence_payload(bar))).encode()
    ).hexdigest()


def _ledger(
    state: ResolutionState,
    kind: str,
    *,
    bar: AidyM1Bar | None = None,
    **extra: Any,
) -> None:
    item: dict[str, Any] = {"kind": kind}
    if bar is not None:
        item["bar"] = _evidence_payload(bar)
    item.update(extra)
    state.evidence_ledger.append(item)
    if len(state.evidence_ledger) > 256:
        state.evidence_ledger = state.evidence_ledger[-256:]


def _update_extrema(state: ResolutionState, bar: AidyM1Bar) -> None:
    if state.status != "open":
        return
    state.max_price = bar.high if state.max_price is None else max(state.max_price, bar.high)
    state.min_price = bar.low if state.min_price is None else min(state.min_price, bar.low)
    state.last_price = bar.close


def _hits_stop(bar: AidyM1Bar, *, side: str, stop: Decimal) -> bool:
    return bar.low <= stop if side == "BUY" else bar.high >= stop


def _hits_target(bar: AidyM1Bar, *, side: str, target: Decimal) -> bool:
    return bar.high >= target if side == "BUY" else bar.low <= target


def _pending_fill(
    bar: AidyM1Bar,
    *,
    geometry: OriginalGeometry,
    last_price: Decimal | None,
) -> tuple[Decimal | None, str | None]:
    order_type = geometry.order_type
    side = geometry.side
    low, high = geometry.entry_low, geometry.entry_high
    point = low
    if order_type == "zone":
        intersection_low = max(low, bar.low)
        intersection_high = min(high, bar.high)
        if intersection_low <= intersection_high:
            return (intersection_high if side == "BUY" else intersection_low), None
        if last_price is not None:
            if side == "BUY" and last_price > high and bar.open < low:
                return None, "aidy_m1_zone_gap_through_entry"
            if side == "SELL" and last_price < low and bar.open > high:
                return None, "aidy_m1_zone_gap_through_entry"
        return None, None
    if low != high:
        return None, "aidy_m1_pending_point_geometry_invalid"
    if order_type == "buy_limit":
        if side != "BUY":
            return None, "aidy_m1_pending_side_mismatch"
        if bar.open <= point:
            return bar.open, None
        return (point, None) if bar.low <= point else (None, None)
    if order_type == "sell_limit":
        if side != "SELL":
            return None, "aidy_m1_pending_side_mismatch"
        if bar.open >= point:
            return bar.open, None
        return (point, None) if bar.high >= point else (None, None)
    if order_type == "buy_stop":
        if side != "BUY":
            return None, "aidy_m1_pending_side_mismatch"
        if bar.open >= point:
            return bar.open, None
        return (point, None) if bar.high >= point else (None, None)
    if order_type == "sell_stop":
        if side != "SELL":
            return None, "aidy_m1_pending_side_mismatch"
        if bar.open <= point:
            return bar.open, None
        return (point, None) if bar.low <= point else (None, None)
    return None, "aidy_m1_pending_order_type_unsupported"


def _realize_fraction(
    leg: LegState,
    *,
    entry: Decimal,
    initial_stop: Decimal,
    side: str,
    exit_price: Decimal,
    fraction_of_remaining: Decimal,
    reason: str,
    when: datetime,
) -> None:
    if fraction_of_remaining <= 0 or fraction_of_remaining > 1:
        raise ValueError("aidy_partial_fraction_invalid")
    if leg.remaining_fraction <= 0 or leg.status not in {"open", "pending"}:
        return
    closed_fraction = leg.remaining_fraction * fraction_of_remaining
    leg.realized_r += _leg_r(
        entry=entry,
        exit_price=exit_price,
        initial_stop=initial_stop,
        side=side,
    ) * closed_fraction
    leg.remaining_fraction -= closed_fraction
    leg.exit_reason = reason
    leg.exit_price = exit_price
    if leg.remaining_fraction == 0:
        leg.status = "closed"
        leg.closed_at = when


def _block(
    state: ResolutionState,
    *,
    reason: str,
    when: datetime,
    note: str | None = None,
    bar: AidyM1Bar | None = None,
) -> None:
    if state.score_block_reason is None:
        state.score_block_reason = reason
    state.note = note or state.note
    state.close_reason = reason
    state.closed_at = when
    state.status = "closed"
    state.terminal = True
    for leg in state.legs:
        if leg.status in {"pending", "open"}:
            leg.status = "cancelled"
            leg.exit_reason = reason
            leg.closed_at = when
            leg.remaining_fraction = Decimal("0")
    _ledger(state, "score_blocked", bar=bar, reason=reason, note=state.note)


def _close_at_stop(
    state: ResolutionState,
    *,
    geometry: OriginalGeometry,
    reason: str,
    when: datetime,
    bar: AidyM1Bar,
) -> None:
    if state.entry_price is None:
        raise ValueError("aidy_stop_before_entry")
    for leg in state.legs:
        if leg.status == "open" and leg.remaining_fraction > 0:
            _realize_fraction(
                leg,
                entry=state.entry_price,
                initial_stop=geometry.initial_stop,
                side=geometry.side,
                exit_price=state.effective_stop,
                fraction_of_remaining=Decimal("1"),
                reason=reason,
                when=when,
            )
    state.status = "closed"
    state.close_reason = reason
    state.closed_at = when
    state.terminal = True
    state.last_price = state.effective_stop
    _ledger(state, "stop", bar=bar, reason=reason, stop=str(state.effective_stop))


def _all_legs_terminal(state: ResolutionState) -> bool:
    return all(
        leg.status in {"closed", "cancelled"} or leg.remaining_fraction == 0
        for leg in state.legs
    )


def _close_if_done(state: ResolutionState, *, when: datetime, reason: str) -> None:
    if _all_legs_terminal(state) and not state.terminal:
        state.status = "closed"
        state.close_reason = reason
        state.closed_at = when
        state.terminal = True


def _target_leg_indexes(target: str, legs: list[LegState]) -> set[int]:
    normalized = target.lower().strip()
    tp = _TP_RE.search(normalized)
    if tp is not None:
        return {int(tp.group(1))}
    if "runner" in normalized:
        return {leg.tp_index for leg in legs if leg.is_runner}
    return {leg.tp_index for leg in legs}


def _trade_scope_applies(
    target: str,
    *,
    geometry: OriginalGeometry,
    sibling_entries: list[tuple[int, Decimal]],
) -> bool:
    normalized = target.lower().strip()
    if normalized in {
        "",
        "all",
        "remaining",
        "pending_layers",
        "partial_tp1",
        "tp1",
        "runner",
    }:
        return True
    entry_match = _ENTRY_RE.search(normalized)
    if entry_match is not None:
        return int(entry_match.group(1)) == geometry.entry_index
    price_match = _ENTRY_PRICE_RE.search(normalized)
    if price_match is not None:
        reference = (
            geometry.entry_low
            if geometry.entry_low == geometry.entry_high
            else geometry.entry_high
        )
        return reference == Decimal(price_match.group(1))
    first_match = _FIRST_N_RE.fullmatch(normalized)
    if first_match is not None:
        selected = sorted(index for index, _ in sibling_entries)[: int(first_match.group(1))]
        return geometry.entry_index in selected
    ordered = sorted(
        sibling_entries,
        key=lambda item: item[1],
        reverse=(geometry.side == "BUY"),
    )
    worst_match = _WORST_N_RE.fullmatch(normalized)
    if worst_match is not None:
        selected = {index for index, _ in ordered[: int(worst_match.group(1))]}
        return geometry.entry_index in selected
    if normalized in {"best_entry", "all_but_best"} or normalized.startswith(
        "best_entry_risk_free_"
    ):
        best_index = ordered[-1][0] if ordered else geometry.entry_index
        return (
            geometry.entry_index != best_index
            if normalized == "all_but_best"
            else geometry.entry_index == best_index
        )
    return True


def _event_actions(event: dict[str, Any]) -> list[dict[str, Any]]:
    payload = dict(event.get("aggregate_result") or {})
    revised = dict(payload.get("revised_instruction") or {})
    actions = list(revised.get("management_actions") or [])
    if not actions and revised.get("update_type"):
        actions = [
            {
                "type": revised.get("update_type"),
                "target": revised.get("update_target") or "all",
                "value": revised.get("update_value"),
            }
        ]
    return [dict(item) for item in actions if isinstance(item, dict)]


def _event_price_active(
    state: ResolutionState,
    *,
    geometry: OriginalGeometry,
    event: dict[str, Any],
    bar: AidyM1Bar,
    sibling_entries: list[tuple[int, Decimal]],
) -> bool:
    for action in _event_actions(event):
        kind = str(action.get("type") or "").lower()
        target = str(action.get("target") or "all")
        if not _trade_scope_applies(
            target,
            geometry=geometry,
            sibling_entries=sibling_entries,
        ):
            continue
        if state.status == "pending" and kind in {"cancel", "cancel_pending"}:
            fill, _ = _pending_fill(
                bar,
                geometry=geometry,
                last_price=state.last_price,
            )
            if fill is not None:
                return True
        if state.status != "open" or state.entry_price is None:
            continue
        if _hits_stop(bar, side=geometry.side, stop=state.effective_stop):
            return True
        if any(
            leg.status == "open"
            and leg.effective_target is not None
            and _hits_target(bar, side=geometry.side, target=leg.effective_target)
            for leg in state.legs
        ):
            return True
        value = _decimal(action.get("value"), allow_none=True)
        if kind in {"move_to_break_even", "breakeven", "break_even"}:
            if _hits_stop(bar, side=geometry.side, stop=state.entry_price):
                return True
        elif kind in {"edit_stop_loss", "move_stop_loss"} and value is not None:
            if _hits_stop(bar, side=geometry.side, stop=value):
                return True
        elif kind in {"edit_take_profit", "move_take_profit"} and value is not None:
            if _hits_target(bar, side=geometry.side, target=value):
                return True
        elif kind in {"close", "close_trade", "close_half"} and value is None:
            return True
    return False


def _apply_actions(
    state: ResolutionState,
    *,
    geometry: OriginalGeometry,
    event: dict[str, Any],
    sibling_entries: list[tuple[int, Decimal]],
) -> bool:
    occurred_at = _utc(event["occurred_at"])
    for action in _event_actions(event):
        kind = str(action.get("type") or "").lower()
        target = str(action.get("target") or "all")
        if not _trade_scope_applies(
            target,
            geometry=geometry,
            sibling_entries=sibling_entries,
        ):
            continue
        value = _decimal(action.get("value"), allow_none=True)
        if kind in {"move_to_break_even", "breakeven", "break_even"}:
            if state.status == "open" and state.entry_price is not None:
                state.effective_stop = state.entry_price
                _ledger(
                    state,
                    "management",
                    action="break_even",
                    occurred_at=occurred_at.isoformat(),
                )
            continue
        if kind in {"edit_stop_loss", "move_stop_loss"}:
            if value is None or value <= 0:
                _block(
                    state,
                    reason="aidy_m1_management_value_invalid",
                    when=occurred_at,
                )
                return False
            if state.status == "open":
                state.effective_stop = value
                _ledger(
                    state,
                    "management",
                    action="move_stop",
                    value=str(value),
                    occurred_at=occurred_at.isoformat(),
                )
            continue
        if kind in {"edit_take_profit", "move_take_profit"}:
            if value is None or value <= 0:
                _block(
                    state,
                    reason="aidy_m1_management_value_invalid",
                    when=occurred_at,
                )
                return False
            tp_match = _TP_RE.search(target)
            if tp_match is None:
                continue
            selected_index = int(tp_match.group(1))
            for leg in state.legs:
                if leg.tp_index == selected_index and leg.status in {"pending", "open"}:
                    leg.effective_target = value
                    leg.is_runner = False
            _ledger(
                state,
                "management",
                action="move_target",
                target=target,
                value=str(value),
                occurred_at=occurred_at.isoformat(),
            )
            continue
        if kind in {"cancel", "cancel_pending"}:
            if state.status == "pending":
                state.status = "cancelled"
                state.close_reason = "provider_cancelled_before_entry"
                state.closed_at = occurred_at
                state.terminal = True
                for leg in state.legs:
                    if leg.status == "pending":
                        leg.status = "cancelled"
                        leg.exit_reason = state.close_reason
                        leg.closed_at = occurred_at
                        leg.remaining_fraction = Decimal("0")
                _ledger(
                    state,
                    "management",
                    action="cancel_pending",
                    occurred_at=occurred_at.isoformat(),
                )
                return False
            continue
        if kind in {"close", "close_trade", "close_half"}:
            partial = kind == "close_half" or "partial" in target.lower()
            if state.status == "pending":
                if partial:
                    continue
                state.status = "cancelled"
                state.close_reason = "provider_closed_before_entry"
                state.closed_at = occurred_at
                state.terminal = True
                for leg in state.legs:
                    if leg.status == "pending":
                        leg.status = "cancelled"
                        leg.exit_reason = state.close_reason
                        leg.closed_at = occurred_at
                        leg.remaining_fraction = Decimal("0")
                _ledger(
                    state,
                    "management",
                    action="close_pending",
                    target=target,
                    occurred_at=occurred_at.isoformat(),
                )
                return False
            if state.status != "open" or state.entry_price is None:
                continue
            if value is None or value <= 0:
                _block(
                    state,
                    reason="aidy_m1_management_exit_price_unobserved",
                    when=occurred_at,
                    note=f"management_close_without_explicit_price:{target}",
                )
                return False
            fraction = Decimal("0.5") if partial else Decimal("1")
            indexes = _target_leg_indexes(target, state.legs)
            for leg in state.legs:
                if leg.tp_index not in indexes or leg.status != "open":
                    continue
                _realize_fraction(
                    leg,
                    entry=state.entry_price,
                    initial_stop=geometry.initial_stop,
                    side=geometry.side,
                    exit_price=value,
                    fraction_of_remaining=fraction,
                    reason="provider_close_partial" if fraction < 1 else "provider_close",
                    when=occurred_at,
                )
            _ledger(
                state,
                "management",
                action=kind,
                target=target,
                value=str(value),
                occurred_at=occurred_at.isoformat(),
            )
            _close_if_done(state, when=occurred_at, reason="provider_close")
            if state.terminal:
                return False
            continue
        _block(
            state,
            reason="aidy_m1_management_event_unsupported",
            when=occurred_at,
            note=f"unsupported_management_action:{kind or 'unknown'}",
        )
        return False
    return not state.terminal


def _initial_state(
    *,
    geometry: OriginalGeometry,
    leg_ids: dict[int, UUID],
    signal_posted_at: datetime,
    lifecycle_mark: str,
) -> ResolutionState:
    legs = [
        LegState(
            id=leg_ids[index],
            tp_index=index,
            original_target=geometry.targets[index],
            effective_target=geometry.targets[index],
            is_runner=index in geometry.runners,
        )
        for index in sorted(geometry.targets)
    ]
    state = ResolutionState(
        status="pending",
        entry_price=None,
        opened_at=None,
        effective_stop=geometry.initial_stop,
        legs=legs,
        market_cursor=None,
        lifecycle_watermark=lifecycle_mark,
    )
    if geometry.order_type == "market":
        if geometry.entry_low != geometry.entry_high:
            _block(
                state,
                reason="aidy_m1_market_entry_not_exact",
                when=signal_posted_at,
            )
            return state
        state.status = "open"
        state.entry_price = geometry.entry_low
        state.opened_at = signal_posted_at
        for leg in state.legs:
            leg.status = "open"
            leg.opened_at = signal_posted_at
        _ledger(
            state,
            "market_entry",
            entry_price=str(state.entry_price),
            occurred_at=signal_posted_at.isoformat(),
        )
    return state


def _critical_touches(
    bar: AidyM1Bar,
    *,
    state: ResolutionState,
    geometry: OriginalGeometry,
) -> tuple[bool, bool]:
    stop_hit = state.status == "open" and _hits_stop(
        bar,
        side=geometry.side,
        stop=state.effective_stop,
    )
    target_hit = any(
        leg.status == "open"
        and leg.effective_target is not None
        and _hits_target(bar, side=geometry.side, target=leg.effective_target)
        for leg in state.legs
    )
    return stop_hit, target_hit


def replay_bars(
    *,
    state: ResolutionState,
    geometry: OriginalGeometry,
    events: list[dict[str, Any]],
    bars: list[AidyM1Bar],
    signal_posted_at: datetime,
    sibling_entries: list[tuple[int, Decimal]],
    full_replay: bool,
) -> ResolutionState:
    """Replay contiguous completed M1 evidence and append-only provider lifecycle events."""
    signal_posted_at = _utc(signal_posted_at)
    ordered_events = sorted(
        events,
        key=lambda item: (
            _utc(item["occurred_at"]),
            _utc(item["created_at"]),
            str(item["id"]),
        ),
    )
    if any(_utc(item["occurred_at"]) < signal_posted_at for item in ordered_events):
        _block(state, reason="aidy_lifecycle_before_signal", when=signal_posted_at)
        return state

    event_index = 0
    if not full_replay and state.market_cursor is not None:
        already_through = _utc(state.market_cursor) + timedelta(minutes=1)
        while (
            event_index < len(ordered_events)
            and _utc(ordered_events[event_index]["occurred_at"]) < already_through
        ):
            event_index += 1

    signal_minute = _minute_floor(signal_posted_at)
    partial_signal_minute = signal_posted_at != signal_minute

    for bar in bars:
        if state.terminal:
            return state
        bar_open = _utc(bar.open_time_utc)
        bar_end = bar_open + timedelta(minutes=1)
        _chain_bar(state, bar)

        while (
            event_index < len(ordered_events)
            and _utc(ordered_events[event_index]["occurred_at"]) <= bar_open
        ):
            if not _apply_actions(
                state,
                geometry=geometry,
                event=ordered_events[event_index],
                sibling_entries=sibling_entries,
            ):
                if state.market_cursor is None:
                    state.market_cursor = bar_open
                return state
            event_index += 1

        in_bar_events: list[dict[str, Any]] = []
        probe = event_index
        while (
            probe < len(ordered_events)
            and _utc(ordered_events[probe]["occurred_at"]) < bar_end
        ):
            in_bar_events.append(ordered_events[probe])
            probe += 1

        if partial_signal_minute and bar_open == signal_minute:
            if state.status == "open":
                stop_hit, target_hit = _critical_touches(
                    bar,
                    state=state,
                    geometry=geometry,
                )
                if stop_hit or target_hit:
                    _block(
                        state,
                        reason="aidy_m1_signal_minute_ambiguous",
                        when=bar_end,
                        note="critical_level_touched_inside_partial_signal_minute",
                        bar=bar,
                    )
                    state.market_cursor = bar_open
                    return state
                state.last_price = bar.close
            elif state.status == "pending":
                fill, fill_error = _pending_fill(
                    bar,
                    geometry=geometry,
                    last_price=state.last_price,
                )
                any_target = any(
                    target is not None
                    and _hits_target(bar, side=geometry.side, target=target)
                    for target in geometry.targets.values()
                )
                stop_touch = _hits_stop(
                    bar,
                    side=geometry.side,
                    stop=geometry.initial_stop,
                )
                if fill_error or fill is not None or stop_touch or any_target:
                    _block(
                        state,
                        reason="aidy_m1_signal_minute_ambiguous",
                        when=bar_end,
                        note=fill_error or "entry_or_exit_level_active_inside_partial_signal_minute",
                        bar=bar,
                    )
                    state.market_cursor = bar_open
                    return state
                state.last_price = bar.close
            for event in in_bar_events:
                if _event_price_active(
                    state,
                    geometry=geometry,
                    event=event,
                    bar=bar,
                    sibling_entries=sibling_entries,
                ):
                    _block(
                        state,
                        reason="aidy_m1_management_bar_ambiguous",
                        when=bar_end,
                        note="management_inside_partial_signal_minute",
                        bar=bar,
                    )
                    state.market_cursor = bar_open
                    return state
                if not _apply_actions(
                    state,
                    geometry=geometry,
                    event=event,
                    sibling_entries=sibling_entries,
                ):
                    state.market_cursor = bar_open
                    return state
                event_index += 1
            state.market_cursor = bar_open
            continue

        for event in in_bar_events:
            if _event_price_active(
                state,
                geometry=geometry,
                event=event,
                bar=bar,
                sibling_entries=sibling_entries,
            ):
                _block(
                    state,
                    reason="aidy_m1_management_bar_ambiguous",
                    when=bar_end,
                    note="management_instruction_inside_price_active_m1_bar",
                    bar=bar,
                )
                state.market_cursor = bar_open
                return state

        opened_this_bar = False
        if state.status == "pending":
            fill, fill_error = _pending_fill(
                bar,
                geometry=geometry,
                last_price=state.last_price,
            )
            if fill_error is not None:
                _block(state, reason=fill_error, when=bar_end, bar=bar)
                state.market_cursor = bar_open
                return state
            if fill is not None:
                state.status = "open"
                state.entry_price = fill
                state.opened_at = bar_open
                state.effective_stop = geometry.initial_stop
                opened_this_bar = True
                for leg in state.legs:
                    leg.status = "open"
                    leg.opened_at = bar_open
                _ledger(
                    state,
                    "pending_entry",
                    bar=bar,
                    entry_price=str(fill),
                    order_type=geometry.order_type,
                )
            else:
                state.last_price = bar.close

        if state.status == "open" and state.entry_price is not None:
            _update_extrema(state, bar)
            stop_hit, target_hit = _critical_touches(
                bar,
                state=state,
                geometry=geometry,
            )
            if opened_this_bar and (stop_hit or target_hit):
                reason = (
                    "aidy_m1_entry_stop_and_target_sequence_ambiguous"
                    if stop_hit and target_hit
                    else "aidy_m1_entry_stop_sequence_ambiguous"
                    if stop_hit
                    else "aidy_m1_entry_target_sequence_ambiguous"
                )
                _block(
                    state,
                    reason=reason,
                    when=bar_end,
                    note="entry_and_exit_order_unknown_inside_entry_m1_bar",
                    bar=bar,
                )
                state.market_cursor = bar_open
                return state

            hit_legs = [
                leg
                for leg in state.legs
                if leg.status == "open"
                and leg.effective_target is not None
                and _hits_target(
                    bar,
                    side=geometry.side,
                    target=leg.effective_target,
                )
            ]
            if stop_hit and hit_legs:
                _close_at_stop(
                    state,
                    geometry=geometry,
                    reason="aidy_m1_ambiguous_worst_case_stop",
                    when=bar_end,
                    bar=bar,
                )
                state.note = "within_bar_sl_and_tp_touched_stop_assumed_first"
                state.market_cursor = bar_open
                return state
            if stop_hit:
                _close_at_stop(
                    state,
                    geometry=geometry,
                    reason="shadow_stop",
                    when=bar_end,
                    bar=bar,
                )
                state.market_cursor = bar_open
                return state
            for leg in hit_legs:
                assert leg.effective_target is not None
                _realize_fraction(
                    leg,
                    entry=state.entry_price,
                    initial_stop=geometry.initial_stop,
                    side=geometry.side,
                    exit_price=leg.effective_target,
                    fraction_of_remaining=Decimal("1"),
                    reason="target",
                    when=bar_end,
                )
                _ledger(
                    state,
                    "target",
                    bar=bar,
                    tp_index=leg.tp_index,
                    target=str(leg.effective_target),
                )
            _close_if_done(state, when=bar_end, reason="all_targets_hit")
            if state.terminal:
                state.market_cursor = bar_open
                return state

        for event in in_bar_events:
            if not _apply_actions(
                state,
                geometry=geometry,
                event=event,
                sibling_entries=sibling_entries,
            ):
                state.market_cursor = bar_open
                return state
            event_index += 1

        state.market_cursor = bar_open

    return state


def contiguous_bars(
    window: AidyM1Window,
    *,
    after_cursor: datetime | None,
) -> tuple[list[AidyM1Bar], datetime | None]:
    by_time = {bar.open_time_utc: bar for bar in window.bars}
    first_missing: datetime | None = None
    usable: list[AidyM1Bar] = []
    cursor = _utc(after_cursor) if after_cursor is not None else None
    for expected in window.expected_open_times:
        bar = by_time.get(expected)
        if bar is None:
            first_missing = expected
            break
        if cursor is None or expected > cursor:
            usable.append(bar)
    return usable, first_missing


class AidyShadowResolver:
    """Single-writer deterministic replay adapter for intraday/swing Provider Lab rows."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        client: AidyMarketClient,
    ) -> None:
        self._session_factory = session_factory
        self._client = client

    def _candidate_ids(self) -> list[UUID]:
        with self._session_factory() as session:
            values = session.execute(
                text(
                    """
                    SELECT id FROM shadow_trades
                    WHERE provider_style IN ('intraday','swing_or_sparse')
                      AND NOT COALESCE(aidy_terminal,false)
                      AND (
                        score_exclusion_reason IN (
                            'market_data_not_observed',
                            'aidy_m1_revalidation_required',
                            'outcome_pending_aidy_m1'
                        )
                        OR quote_mode='aidy_m1'
                      )
                    ORDER BY signal_posted_at,id
                    """
                )
            ).scalars().all()
        return [UUID(str(value)) for value in values]

    def _load(
        self,
        trade_id: UUID,
    ) -> tuple[
        dict[str, Any],
        OriginalGeometry,
        list[LegState],
        list[dict[str, Any]],
        list[tuple[int, Decimal]],
        str,
    ]:
        with self._session_factory() as session:
            trade = dict(
                session.execute(
                    text("SELECT * FROM shadow_trades WHERE id=:id"),
                    {"id": trade_id},
                ).mappings().one()
            )
            geometry = OriginalGeometry.from_payload(trade.get("aidy_original_geometry"))
            leg_rows = list(
                session.execute(
                    text(
                        "SELECT * FROM shadow_trade_legs "
                        "WHERE shadow_trade_id=:id ORDER BY tp_index"
                    ),
                    {"id": trade_id},
                ).mappings()
            )
            event_rows = [
                dict(row)
                for row in session.execute(
                    text(
                        """
                        SELECT id,event_key,event_type,occurred_at,created_at,aggregate_result
                        FROM signal_lifecycle_events
                        WHERE signal_id=:signal_id AND origin='provider_update'
                        ORDER BY occurred_at,created_at,id
                        """
                    ),
                    {"signal_id": trade["signal_id"]},
                ).mappings()
            ]
            sibling_rows = list(
                session.execute(
                    text(
                        """
                        SELECT entry_index,aidy_original_geometry
                        FROM shadow_trades
                        WHERE signal_id=:signal_id
                        ORDER BY entry_index
                        """
                    ),
                    {"signal_id": trade["signal_id"]},
                ).mappings()
            )
        mark = lifecycle_watermark(event_rows)
        legs = [
            LegState(
                id=UUID(str(row["id"])),
                tp_index=int(row["tp_index"]),
                original_target=_decimal(
                    row.get("aidy_original_target"),
                    allow_none=True,
                ),
                effective_target=_decimal(
                    row.get("aidy_effective_target"),
                    allow_none=True,
                ),
                is_runner=bool(row["is_runner"]),
                status=str(row["status"]),
                remaining_fraction=_decimal(row["remaining_fraction"])
                or Decimal("0"),
                realized_r=_decimal(row["quality_r"]) or Decimal("0"),
                exit_reason=row["exit_reason"],
                exit_price=_decimal(row["exit_price"], allow_none=True),
                opened_at=row["opened_at"],
                closed_at=row["closed_at"],
            )
            for row in leg_rows
        ]
        sibling_entries: list[tuple[int, Decimal]] = []
        for row in sibling_rows:
            payload = row["aidy_original_geometry"]
            if not isinstance(payload, dict):
                continue
            low = _price(payload.get("entry_low"))
            high = _price(payload.get("entry_high"))
            side = str(payload.get("side") or geometry.side).upper()
            ref = max(low, high) if side == "BUY" else min(low, high)
            sibling_entries.append((int(row["entry_index"]), ref))
        return trade, geometry, legs, event_rows, sibling_entries, mark

    def _state_from_persisted(
        self,
        trade: dict[str, Any],
        legs: list[LegState],
        mark: str,
    ) -> ResolutionState:
        raw_ledger = trade.get("aidy_resolution_evidence")
        ledger = list(raw_ledger) if isinstance(raw_ledger, list) else []
        stop = _decimal(trade.get("aidy_effective_stop"), allow_none=True)
        if stop is None:
            stop = _price(trade["initial_stop"])
        return ResolutionState(
            status=str(trade["status"]),
            entry_price=_decimal(trade.get("entry_price"), allow_none=True),
            opened_at=trade.get("opened_at"),
            effective_stop=stop,
            legs=legs,
            market_cursor=trade.get("aidy_m1_cursor_at"),
            lifecycle_watermark=mark,
            score_block_reason=(
                str(trade.get("score_exclusion_reason"))
                if bool(trade.get("aidy_score_blocked"))
                else None
            ),
            note=trade.get("aidy_resolution_note"),
            close_reason=trade.get("close_reason"),
            closed_at=trade.get("closed_at"),
            last_price=_decimal(trade.get("last_price"), allow_none=True),
            max_price=_decimal(trade.get("max_price"), allow_none=True),
            min_price=_decimal(trade.get("min_price"), allow_none=True),
            terminal=bool(trade.get("aidy_terminal")),
            evidence_digest=trade.get("aidy_evidence_digest"),
            evidence_ledger=ledger,
        )

    def _current_lifecycle_watermark(
        self,
        session: Session,
        signal_id: UUID,
    ) -> str:
        rows = [
            dict(row)
            for row in session.execute(
                text(
                    """
                    SELECT id,event_key,occurred_at,created_at
                    FROM signal_lifecycle_events
                    WHERE signal_id=:signal_id AND origin='provider_update'
                    ORDER BY created_at,id
                    """
                ),
                {"signal_id": signal_id},
            ).mappings()
        ]
        return lifecycle_watermark(rows)

    def _persist(
        self,
        *,
        trade: dict[str, Any],
        state: ResolutionState,
        expected_version: int,
        expected_cursor: datetime | None,
        expected_lifecycle_watermark: str | None,
        loaded_lifecycle_watermark: str,
    ) -> None:
        quality_r = sum(
            (leg.realized_r for leg in state.legs),
            Decimal("0"),
        )
        pnl = sum(
            (_benchmark_pnl_usd(leg.realized_r) for leg in state.legs),
            Decimal("0"),
        )
        hit_targets = [
            leg.tp_index
            for leg in state.legs
            if leg.status == "closed"
            and leg.exit_reason == "target"
            and not leg.is_runner
        ]
        with self._session_factory() as session:
            current_mark = self._current_lifecycle_watermark(
                session,
                UUID(str(trade["signal_id"])),
            )
            if current_mark != loaded_lifecycle_watermark:
                session.rollback()
                raise ConcurrentResolution("aidy_lifecycle_changed_during_replay")
            result = session.execute(
                text(
                    """
                    UPDATE shadow_trades
                    SET status=:status,entry_price=:entry_price,opened_at=:opened_at,
                        current_stop=:stop,aidy_effective_stop=:stop,quote_mode=:quote_mode,
                        score_eligible=:eligible,score_exclusion_reason=:exclusion,
                        aidy_resolution_note=:note,aidy_m1_cursor_at=:cursor,
                        aidy_lifecycle_watermark=:new_lifecycle_mark,
                        aidy_state_version=aidy_state_version+1,
                        aidy_terminal=:terminal,aidy_score_blocked=:score_blocked,
                        aidy_evidence_digest=:evidence_digest,
                        aidy_resolution_evidence=CAST(:evidence AS jsonb),
                        closed_at=:closed_at,close_reason=:close_reason,last_price=:last_price,
                        max_price=:max_price,min_price=:min_price,
                        hit_targets=CAST(:hit_targets AS jsonb),quality_r_multiple=:quality_r,
                        realized_percent=:quality_r,benchmark_pnl_usd=:pnl,
                        pnl_percent=CASE WHEN :eligible THEN :quality_r ELSE NULL END,
                        updated_at=now()
                    WHERE id=:id
                      AND aidy_state_version=:expected_version
                      AND aidy_m1_cursor_at IS NOT DISTINCT FROM :expected_cursor
                      AND aidy_lifecycle_watermark IS NOT DISTINCT FROM :expected_lifecycle_mark
                      AND NOT COALESCE(aidy_terminal,false)
                    """
                ),
                {
                    "id": trade["id"],
                    "status": state.status,
                    "entry_price": state.entry_price,
                    "opened_at": state.opened_at,
                    "stop": state.effective_stop,
                    "quote_mode": AIDY_QUOTE_MODE,
                    "eligible": state.score_eligible,
                    "exclusion": state.exclusion_reason,
                    "note": state.note,
                    "cursor": state.market_cursor,
                    "new_lifecycle_mark": loaded_lifecycle_watermark,
                    "terminal": state.terminal,
                    "score_blocked": state.score_block_reason is not None,
                    "evidence_digest": state.evidence_digest,
                    "evidence": _canonical(state.evidence_ledger),
                    "closed_at": state.closed_at,
                    "close_reason": state.close_reason,
                    "last_price": state.last_price,
                    "max_price": state.max_price,
                    "min_price": state.min_price,
                    "hit_targets": _canonical(sorted(hit_targets)),
                    "quality_r": quality_r,
                    "pnl": pnl,
                    "expected_version": expected_version,
                    "expected_cursor": expected_cursor,
                    "expected_lifecycle_mark": expected_lifecycle_watermark,
                },
            )
            if int(result.rowcount or 0) != 1:
                session.rollback()
                raise ConcurrentResolution("aidy_shadow_state_cas_failed")
            for leg in state.legs:
                session.execute(
                    text(
                        """
                        UPDATE shadow_trade_legs
                        SET status=:status,remaining_fraction=:remaining,exit_reason=:reason,
                            exit_price=:exit_price,quality_r=:quality_r,benchmark_pnl_usd=:pnl,
                            aidy_effective_target=:effective_target,
                            opened_at=:opened_at,closed_at=:closed_at,updated_at=now()
                        WHERE id=:id
                        """
                    ),
                    {
                        "id": leg.id,
                        "status": leg.status,
                        "remaining": leg.remaining_fraction,
                        "reason": leg.exit_reason,
                        "exit_price": leg.exit_price,
                        "quality_r": leg.realized_r,
                        "pnl": _benchmark_pnl_usd(leg.realized_r),
                        "effective_target": leg.effective_target,
                        "opened_at": leg.opened_at,
                        "closed_at": leg.closed_at,
                    },
                )
            session.commit()

    async def resolve_trade(
        self,
        trade_id: UUID,
        *,
        now: datetime | None = None,
    ) -> bool:
        (
            trade,
            geometry,
            legs,
            events,
            sibling_entries,
            loaded_mark,
        ) = await asyncio.to_thread(self._load, trade_id)
        if (
            str(trade.get("provider_style")) not in _SUPPORTED_STYLES
            or bool(trade.get("aidy_terminal"))
        ):
            return False
        posted_at = trade.get("signal_posted_at")
        if not isinstance(posted_at, datetime):
            return False
        posted_at = _utc(posted_at)
        expected_version = int(trade.get("aidy_state_version") or 0)
        expected_cursor = trade.get("aidy_m1_cursor_at")
        expected_mark = trade.get("aidy_lifecycle_watermark")
        revalidation = trade.get("score_exclusion_reason") in {
            "market_data_not_observed",
            "aidy_m1_revalidation_required",
        }
        lifecycle_changed = expected_mark != loaded_mark
        full_replay = revalidation or expected_cursor is None or lifecycle_changed
        if full_replay:
            leg_ids = {leg.tp_index: leg.id for leg in legs}
            state = _initial_state(
                geometry=geometry,
                leg_ids=leg_ids,
                signal_posted_at=posted_at,
                lifecycle_mark=loaded_mark,
            )
            start = _minute_floor(posted_at)
        else:
            state = self._state_from_persisted(trade, legs, loaded_mark)
            assert expected_cursor is not None
            start = _utc(expected_cursor)

        if state.terminal:
            return False
        end_now = _minute_floor(now or datetime.now(UTC))
        end = min(end_now, start + _MAX_WINDOW)
        if start >= end:
            return False
        window = await self._client.fetch_m1(start=start, end=end)
        usable, first_missing = contiguous_bars(
            window,
            after_cursor=None if full_replay else expected_cursor,
        )
        if not usable:
            if first_missing is not None:
                logger.info(
                    "AIDY M1 continuity blocked trade=%s missing=%s",
                    trade_id,
                    first_missing.isoformat(),
                )
            return False
        state = replay_bars(
            state=state,
            geometry=geometry,
            events=events,
            bars=usable,
            signal_posted_at=posted_at,
            sibling_entries=sibling_entries,
            full_replay=full_replay,
        )
        if first_missing is not None:
            state.note = (
                "aidy_m1_waiting_for_missing_minute:"
                f"{first_missing.isoformat()}"
            )
            _ledger(
                state,
                "continuity_stop",
                missing_open_time=first_missing.isoformat(),
            )
        state.lifecycle_watermark = loaded_mark
        await asyncio.to_thread(
            self._persist,
            trade=trade,
            state=state,
            expected_version=expected_version,
            expected_cursor=expected_cursor,
            expected_lifecycle_watermark=expected_mark,
            loaded_lifecycle_watermark=loaded_mark,
        )
        return True

    async def resolve_once(
        self,
        *,
        now: datetime | None = None,
    ) -> tuple[int, int]:
        processed = 0
        failures = 0
        for trade_id in await asyncio.to_thread(self._candidate_ids):
            try:
                processed += int(await self.resolve_trade(trade_id, now=now))
            except asyncio.CancelledError:
                raise
            except ConcurrentResolution:
                failures += 1
                logger.info(
                    "AIDY Provider Lab replay lost CAS race safely trade=%s",
                    trade_id,
                )
            except (httpx.HTTPError, RuntimeError, TypeError, ValueError):
                failures += 1
                logger.exception(
                    "AIDY Provider Lab deterministic replay failed safely trade=%s",
                    trade_id,
                )
        return processed, failures


__all__ = [
    "AidyShadowResolver",
    "ConcurrentResolution",
    "LegState",
    "OriginalGeometry",
    "ResolutionState",
    "contiguous_bars",
    "lifecycle_watermark",
    "replay_bars",
]
