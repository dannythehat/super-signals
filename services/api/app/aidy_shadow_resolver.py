from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.aidy_market_client import AIDY_QUOTE_MODE, AidyM1Bar, AidyMarketClient
from app.shadow_trading_v2 import _benchmark_pnl_usd, _decimal, _leg_r

logger = logging.getLogger(__name__)

_SUPPORTED_STYLES = {"intraday", "swing_or_sparse"}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _next_full_minute(value: datetime) -> datetime:
    value = _utc(value)
    return value.replace(second=0, microsecond=0) + timedelta(minutes=1)


def _complete_minute_start(value: datetime) -> datetime:
    value = _utc(value)
    return value.replace(second=0, microsecond=0)


def _touches_entry(*, low: Decimal, high: Decimal, entry_low: Decimal, entry_high: Decimal) -> bool:
    return high >= min(entry_low, entry_high) and low <= max(entry_low, entry_high)


def _stop_hit(*, side: str, low: Decimal, high: Decimal, stop: Decimal) -> bool:
    return low <= stop if side == "BUY" else high >= stop


def _target_hit(*, side: str, low: Decimal, high: Decimal, target: Decimal) -> bool:
    return high >= target if side == "BUY" else low <= target


@dataclass(slots=True)
class LegState:
    id: UUID
    tp_index: int
    target: Decimal | None
    is_runner: bool
    status: str = "pending"
    quality_r: Decimal = Decimal("0")
    exit_reason: str | None = None
    exit_price: Decimal | None = None
    opened_at: datetime | None = None
    closed_at: datetime | None = None


@dataclass(slots=True)
class ResolutionState:
    status: str
    entry_price: Decimal | None
    opened_at: datetime | None
    stop: Decimal
    legs: list[LegState]
    cursor_at: datetime | None
    closed_at: datetime | None = None
    close_reason: str | None = None
    exclusion_reason: str | None = "outcome_pending_aidy_m1"
    note: str | None = None
    last_price: Decimal | None = None
    max_price: Decimal | None = None
    min_price: Decimal | None = None

    @property
    def score_eligible(self) -> bool:
        return self.status == "closed" and self.exclusion_reason is None


def _close_leg(
    leg: LegState,
    *,
    entry: Decimal,
    initial_stop: Decimal,
    side: str,
    exit_price: Decimal,
    reason: str,
    closed_at: datetime,
) -> None:
    leg.status = "closed"
    leg.exit_price = exit_price
    leg.exit_reason = reason
    leg.closed_at = closed_at
    leg.quality_r = _leg_r(
        entry=entry,
        exit_price=exit_price,
        initial_stop=initial_stop,
        side=side,
    )


def _close_remaining_at_stop(
    state: ResolutionState,
    *,
    entry: Decimal,
    initial_stop: Decimal,
    side: str,
    stop: Decimal,
    reason: str,
    closed_at: datetime,
) -> None:
    for leg in state.legs:
        if leg.status == "open":
            _close_leg(
                leg,
                entry=entry,
                initial_stop=initial_stop,
                side=side,
                exit_price=stop,
                reason=reason,
                closed_at=closed_at,
            )
    state.status = "closed"
    state.closed_at = closed_at
    state.close_reason = reason
    state.exclusion_reason = None


def resolve_bars(
    *,
    trade: dict[str, Any],
    legs: list[LegState],
    events: list[dict[str, Any]],
    bars: list[AidyM1Bar],
    reset_from_signal: bool,
) -> ResolutionState:
    side = str(trade["side"])
    initial_stop = _decimal(trade["initial_stop"])
    entry_low = _decimal(trade["entry_low"])
    entry_high = _decimal(trade["entry_high"])
    if initial_stop is None or entry_low is None or entry_high is None:
        raise ValueError("AIDY resolver requires entry bounds and initial stop.")

    if reset_from_signal:
        for leg in legs:
            leg.status = "pending"
            leg.quality_r = Decimal("0")
            leg.exit_reason = None
            leg.exit_price = None
            leg.opened_at = None
            leg.closed_at = None
        state = ResolutionState(
            status="pending",
            entry_price=None,
            opened_at=None,
            stop=initial_stop,
            legs=legs,
            cursor_at=None,
        )
    else:
        state = ResolutionState(
            status=str(trade["status"]),
            entry_price=_decimal(trade["entry_price"]),
            opened_at=trade.get("opened_at"),
            stop=_decimal(trade.get("aidy_effective_stop")) or initial_stop,
            legs=legs,
            cursor_at=trade.get("aidy_m1_cursor_at"),
            closed_at=trade.get("closed_at"),
            close_reason=trade.get("close_reason"),
            exclusion_reason=trade.get("score_exclusion_reason") or "outcome_pending_aidy_m1",
            note=trade.get("aidy_resolution_note"),
            last_price=_decimal(trade.get("last_price")),
            max_price=_decimal(trade.get("max_price")),
            min_price=_decimal(trade.get("min_price")),
        )

    event_index = 0
    ordered_events = sorted(events, key=lambda item: item["occurred_at"])
    order_type = str(trade.get("entry_order_type") or "zone")

    def apply_event(event: dict[str, Any], *, within_bar: bool, bar: AidyM1Bar | None = None) -> bool:
        kind = str(event.get("event_type") or "").lower()
        occurred_at = _utc(event["occurred_at"])
        if kind == "break_even":
            if state.status != "open" or state.entry_price is None:
                return True
            if within_bar and bar is not None:
                relevant = _stop_hit(side=side, low=bar.low, high=bar.high, stop=state.stop)
                relevant = relevant or _stop_hit(
                    side=side, low=bar.low, high=bar.high, stop=state.entry_price
                )
                relevant = relevant or any(
                    leg.target is not None
                    and leg.status == "open"
                    and _target_hit(side=side, low=bar.low, high=bar.high, target=leg.target)
                    for leg in state.legs
                )
                if relevant:
                    state.exclusion_reason = "aidy_m1_management_bar_ambiguous"
                    state.note = "break_even_instruction_inside_price_active_m1_bar"
                    state.cursor_at = bar.open_time_utc
                    return False
            state.stop = state.entry_price
            return True
        if kind == "cancel":
            if state.status != "pending":
                return True
            if within_bar and bar is not None and _touches_entry(
                low=bar.low,
                high=bar.high,
                entry_low=entry_low,
                entry_high=entry_high,
            ):
                state.exclusion_reason = "aidy_m1_management_bar_ambiguous"
                state.note = "cancel_pending_instruction_inside_entry_active_m1_bar"
                state.cursor_at = bar.open_time_utc
                return False
            state.status = "cancelled"
            state.closed_at = occurred_at
            state.close_reason = "provider_cancelled_before_entry"
            state.exclusion_reason = "provider_cancelled_before_entry"
            for leg in state.legs:
                if leg.status == "pending":
                    leg.status = "cancelled"
                    leg.exit_reason = "provider_cancelled_before_entry"
                    leg.closed_at = occurred_at
            return False
        state.exclusion_reason = "aidy_m1_management_event_unsupported"
        state.note = f"unsupported_lifecycle_event:{kind or 'unknown'}"
        return False

    for bar in bars:
        bar_open = _utc(bar.open_time_utc)
        bar_end = bar_open + timedelta(minutes=1)
        while event_index < len(ordered_events) and _utc(ordered_events[event_index]["occurred_at"]) <= bar_open:
            if not apply_event(ordered_events[event_index], within_bar=False):
                return state
            event_index += 1

        in_bar_events: list[dict[str, Any]] = []
        probe = event_index
        while probe < len(ordered_events) and _utc(ordered_events[probe]["occurred_at"]) < bar_end:
            in_bar_events.append(ordered_events[probe])
            probe += 1

        newly_opened = False
        if state.status == "pending":
            if order_type == "market":
                entry = bar.open
                first_targets = [leg.target for leg in state.legs if leg.target is not None]
                passed_stop = (side == "BUY" and entry <= initial_stop) or (
                    side == "SELL" and entry >= initial_stop
                )
                passed_target = bool(first_targets) and (
                    entry >= min(first_targets) if side == "BUY" else entry <= max(first_targets)
                )
                if passed_stop or passed_target:
                    state.status = "missed"
                    state.closed_at = bar_open
                    state.close_reason = (
                        "market_signal_arrived_beyond_stop"
                        if passed_stop
                        else "market_signal_arrived_after_first_target"
                    )
                    state.exclusion_reason = state.close_reason
                    state.cursor_at = bar_open
                    for leg in state.legs:
                        leg.status = "cancelled"
                        leg.exit_reason = state.close_reason
                        leg.closed_at = bar_open
                    return state
            else:
                entered = _touches_entry(
                    low=bar.low,
                    high=bar.high,
                    entry_low=entry_low,
                    entry_high=entry_high,
                )
                if not entered:
                    if _stop_hit(side=side, low=bar.low, high=bar.high, stop=initial_stop):
                        state.status = "missed"
                        state.closed_at = bar_end
                        state.close_reason = "price_passed_stop_before_entry"
                        state.exclusion_reason = state.close_reason
                        state.cursor_at = bar_open
                        for leg in state.legs:
                            leg.status = "cancelled"
                            leg.exit_reason = state.close_reason
                            leg.closed_at = bar_end
                        return state
                    state.cursor_at = bar_open
                    state.last_price = bar.close
                    continue
                entry = max(entry_low, entry_high) if side == "BUY" else min(entry_low, entry_high)

            state.status = "open"
            state.entry_price = entry
            state.opened_at = bar_open
            state.stop = initial_stop
            newly_opened = True
            for leg in state.legs:
                leg.status = "open"
                leg.opened_at = bar_open

        if state.status != "open" or state.entry_price is None:
            state.cursor_at = bar_open
            continue

        stop_hit = _stop_hit(side=side, low=bar.low, high=bar.high, stop=state.stop)
        target_legs = [
            leg
            for leg in state.legs
            if leg.status == "open"
            and leg.target is not None
            and _target_hit(side=side, low=bar.low, high=bar.high, target=leg.target)
        ]

        if stop_hit and target_legs:
            _close_remaining_at_stop(
                state,
                entry=state.entry_price,
                initial_stop=initial_stop,
                side=side,
                stop=state.stop,
                reason="aidy_m1_ambiguous_worst_case_stop",
                closed_at=bar_end,
            )
            state.note = "within_bar_sl_and_tp_touched_stop_assumed_first"
            state.cursor_at = bar_open
            state.last_price = state.stop
            return state

        if stop_hit:
            _close_remaining_at_stop(
                state,
                entry=state.entry_price,
                initial_stop=initial_stop,
                side=side,
                stop=state.stop,
                reason="shadow_stop",
                closed_at=bar_end,
            )
            state.cursor_at = bar_open
            state.last_price = state.stop
            return state

        # A zone entry has no within-bar sequence evidence. Do not grant a target hit
        # in its entry bar; the next full M1 bar is the first scoreable target interval.
        if not (newly_opened and order_type != "market"):
            for leg in target_legs:
                assert leg.target is not None
                _close_leg(
                    leg,
                    entry=state.entry_price,
                    initial_stop=initial_stop,
                    side=side,
                    exit_price=leg.target,
                    reason="target",
                    closed_at=bar_end,
                )

        if all(leg.status in {"closed", "cancelled"} for leg in state.legs):
            state.status = "closed"
            state.closed_at = bar_end
            state.close_reason = "all_targets_hit"
            state.exclusion_reason = None
            state.cursor_at = bar_open
            state.last_price = bar.close
            return state

        for event in in_bar_events:
            if not apply_event(event, within_bar=True, bar=bar):
                return state
            event_index += 1

        state.cursor_at = bar_open
        state.last_price = bar.close
        state.max_price = bar.high if state.max_price is None else max(state.max_price, bar.high)
        state.min_price = bar.low if state.min_price is None else min(state.min_price, bar.low)

    return state


class AidyShadowResolver:
    """Incrementally resolve only intraday/swing Provider Lab trades from AIDY M1."""

    def __init__(self, session_factory: sessionmaker[Session], client: AidyMarketClient) -> None:
        self._session_factory = session_factory
        self._client = client

    def _candidate_ids(self) -> list[UUID]:
        with self._session_factory() as session:
            values = session.execute(
                text(
                    """
                    SELECT id FROM shadow_trades
                    WHERE provider_style IN ('intraday','swing_or_sparse')
                      AND (
                        score_exclusion_reason='market_data_not_observed'
                        OR (quote_mode='aidy_m1' AND status IN ('pending','open'))
                      )
                    ORDER BY signal_posted_at,id
                    """
                )
            ).scalars().all()
        return [UUID(str(value)) for value in values]

    def _load(self, trade_id: UUID) -> tuple[dict[str, Any], list[LegState], list[dict[str, Any]]]:
        with self._session_factory() as session:
            trade = session.execute(
                text("SELECT * FROM shadow_trades WHERE id=:id"), {"id": trade_id}
            ).mappings().one()
            leg_rows = list(
                session.execute(
                    text("SELECT * FROM shadow_trade_legs WHERE shadow_trade_id=:id ORDER BY tp_index"),
                    {"id": trade_id},
                ).mappings()
            )
            cursor = trade["aidy_m1_cursor_at"]
            event_rows = list(
                session.execute(
                    text(
                        """
                        SELECT event_type,occurred_at,aggregate_result
                        FROM signal_lifecycle_events
                        WHERE signal_id=:signal_id
                          AND (:cursor IS NULL OR occurred_at>:cursor)
                        ORDER BY occurred_at,id
                        """
                    ),
                    {"signal_id": trade["signal_id"], "cursor": cursor},
                ).mappings()
            )
        legs = [
            LegState(
                id=UUID(str(row["id"])),
                tp_index=int(row["tp_index"]),
                target=_decimal(row["target_price"]),
                is_runner=bool(row["is_runner"]),
                status=str(row["status"]),
                quality_r=_decimal(row["quality_r"]) or Decimal("0"),
                exit_reason=row["exit_reason"],
                exit_price=_decimal(row["exit_price"]),
                opened_at=row["opened_at"],
                closed_at=row["closed_at"],
            )
            for row in leg_rows
        ]
        events = [dict(row) for row in event_rows]
        return dict(trade), legs, events

    def _persist(self, trade_id: UUID, state: ResolutionState) -> None:
        quality_r = sum((leg.quality_r for leg in state.legs), Decimal("0"))
        pnl = sum((_benchmark_pnl_usd(leg.quality_r) for leg in state.legs), Decimal("0"))
        hit_targets = [
            leg.tp_index
            for leg in state.legs
            if leg.status == "closed" and leg.exit_reason == "target" and not leg.is_runner
        ]
        with self._session_factory() as session:
            for leg in state.legs:
                remaining = Decimal("0") if leg.status in {"closed", "cancelled"} else Decimal("1")
                session.execute(
                    text(
                        """
                        UPDATE shadow_trade_legs
                        SET status=:status,remaining_fraction=:remaining,exit_reason=:reason,
                            exit_price=:exit_price,quality_r=:quality_r,benchmark_pnl_usd=:pnl,
                            opened_at=:opened_at,closed_at=:closed_at,updated_at=now()
                        WHERE id=:id
                        """
                    ),
                    {
                        "id": leg.id,
                        "status": leg.status,
                        "remaining": remaining,
                        "reason": leg.exit_reason,
                        "exit_price": leg.exit_price,
                        "quality_r": leg.quality_r,
                        "pnl": _benchmark_pnl_usd(leg.quality_r),
                        "opened_at": leg.opened_at,
                        "closed_at": leg.closed_at,
                    },
                )
            session.execute(
                text(
                    """
                    UPDATE shadow_trades
                    SET status=:status,entry_price=:entry_price,opened_at=:opened_at,
                        current_stop=:stop,aidy_effective_stop=:stop,
                        quote_mode=:quote_mode,score_eligible=:eligible,
                        score_exclusion_reason=:exclusion,aidy_resolution_note=:note,
                        aidy_m1_cursor_at=:cursor,closed_at=:closed_at,close_reason=:close_reason,
                        last_price=:last_price,max_price=:max_price,min_price=:min_price,
                        hit_targets=CAST(:hit_targets AS jsonb),quality_r_multiple=:quality_r,
                        realized_percent=:quality_r,benchmark_pnl_usd=:pnl,
                        pnl_percent=CASE WHEN :eligible THEN :quality_r ELSE pnl_percent END,
                        updated_at=now()
                    WHERE id=:id
                    """
                ),
                {
                    "id": trade_id,
                    "status": state.status,
                    "entry_price": state.entry_price,
                    "opened_at": state.opened_at,
                    "stop": state.stop,
                    "quote_mode": AIDY_QUOTE_MODE,
                    "eligible": state.score_eligible,
                    "exclusion": state.exclusion_reason,
                    "note": state.note,
                    "cursor": state.cursor_at,
                    "closed_at": state.closed_at,
                    "close_reason": state.close_reason,
                    "last_price": state.last_price,
                    "max_price": state.max_price,
                    "min_price": state.min_price,
                    "hit_targets": json.dumps(sorted(hit_targets)),
                    "quality_r": quality_r,
                    "pnl": pnl,
                },
            )
            session.commit()

    async def resolve_trade(self, trade_id: UUID, *, now: datetime | None = None) -> bool:
        trade, legs, events = await asyncio.to_thread(self._load, trade_id)
        if str(trade.get("provider_style")) not in _SUPPORTED_STYLES:
            return False
        posted_at = trade.get("signal_posted_at")
        if not isinstance(posted_at, datetime):
            return False
        cursor = trade.get("aidy_m1_cursor_at")
        reset = cursor is None and trade.get("score_exclusion_reason") == "market_data_not_observed"
        start = (
            _next_full_minute(posted_at)
            if cursor is None
            else _utc(cursor) + timedelta(minutes=1)
        )
        end = _complete_minute_start(now or datetime.now(UTC))
        if start >= end:
            return False
        bars = await self._client.fetch_m1(start=start, end=end)
        if not bars:
            return False
        state = resolve_bars(
            trade=trade,
            legs=legs,
            events=events,
            bars=bars,
            reset_from_signal=reset,
        )
        await asyncio.to_thread(self._persist, trade_id, state)
        return True

    async def resolve_once(self, *, now: datetime | None = None) -> tuple[int, int]:
        candidates = await asyncio.to_thread(self._candidate_ids)
        processed = 0
        failures = 0
        for trade_id in candidates:
            try:
                processed += int(await self.resolve_trade(trade_id, now=now))
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, RuntimeError, TypeError, ValueError):  # type: ignore[name-defined]
                failures += 1
                logger.exception("AIDY Provider Lab M1 resolution failed safely trade=%s", trade_id)
        return processed, failures
