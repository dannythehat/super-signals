"""Deterministic point-in-time event, liquidity and execution evidence for AIDY.

This layer does not predict price direction. It converts already-known signal geometry,
immutable market/quote context, scheduled-event metadata, and broker execution calibration
available before the signal into a compact decision surface. Unknown evidence stays unknown.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

CONTEXT_VERSION = "aidy_event_liquidity_execution_v1"
_MIN_EXECUTION_SAMPLES = 30


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _utc(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _execution_calibration(raw: dict[str, Any] | None, *, as_of: datetime) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    evidence_as_of = _utc(data.get("evidence_as_of_utc"))
    future = evidence_as_of is not None and evidence_as_of > as_of.astimezone(UTC)
    counts = {
        "entry_slippage_samples": int(data.get("entry_slippage_samples") or 0),
        "exit_slippage_samples": int(data.get("exit_slippage_samples") or 0),
        "contract_value_samples": int(data.get("contract_value_samples") or 0),
        "cash_charge_samples": int(data.get("cash_charge_samples") or 0),
    }
    calibrated = (
        not future
        and all(value >= _MIN_EXECUTION_SAMPLES for value in counts.values())
    )
    return {
        "status": (
            "engineering_calibrated"
            if calibrated
            else ("invalid_future_evidence" if future else "insufficient_samples")
        ),
        **counts,
        "entry_adverse_p50_points": (
            str(data.get("entry_adverse_p50_points"))
            if data.get("entry_adverse_p50_points") is not None
            else None
        ),
        "entry_adverse_p95_points": (
            str(data.get("entry_adverse_p95_points"))
            if data.get("entry_adverse_p95_points") is not None
            else None
        ),
        "exit_adverse_p50_points": (
            str(data.get("exit_adverse_p50_points"))
            if data.get("exit_adverse_p50_points") is not None
            else None
        ),
        "exit_adverse_p95_points": (
            str(data.get("exit_adverse_p95_points"))
            if data.get("exit_adverse_p95_points") is not None
            else None
        ),
        "usd_per_point_per_lot_p50": (
            str(data.get("usd_per_point_per_lot_p50"))
            if data.get("usd_per_point_per_lot_p50") is not None
            else None
        ),
        "cash_charge_per_lot_p50_usd": (
            str(data.get("cash_charge_per_lot_p50_usd"))
            if data.get("cash_charge_per_lot_p50_usd") is not None
            else None
        ),
        "cash_charge_per_lot_p95_usd": (
            str(data.get("cash_charge_per_lot_p95_usd"))
            if data.get("cash_charge_per_lot_p95_usd") is not None
            else None
        ),
        "evidence_as_of_utc": evidence_as_of.isoformat() if evidence_as_of else None,
        "account_environment": "demo",
        "research_only": True,
        "live_money_execution_allowed": False,
    }


def _nearest_scheduled_event(
    events: list[dict[str, Any]], *, as_of: datetime
) -> dict[str, Any] | None:
    candidates: list[tuple[float, dict[str, Any], datetime]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        when = _utc(event.get("time_utc") or event.get("event_time_utc"))
        if when is None:
            continue
        minutes = (when - as_of.astimezone(UTC)).total_seconds() / 60
        candidates.append((abs(minutes), event, when))
    if not candidates:
        return None
    _, event, when = min(candidates, key=lambda item: item[0])
    minutes = int(round((when - as_of.astimezone(UTC)).total_seconds() / 60))
    return {
        "title": str(event.get("title") or ""),
        "country": str(event.get("country") or ""),
        "impact": str(event.get("impact") or ""),
        "time_utc": when.isoformat(),
        "minutes_from_signal": minutes,
        "forecast": str(event.get("forecast") or ""),
        "previous": str(event.get("previous") or ""),
    }


def build_event_liquidity_execution_context(
    *,
    signal_posted_at: datetime,
    side: str,
    entry_low: Any,
    entry_high: Any,
    stop_loss: Any,
    take_profits: list[Any],
    market_context: dict[str, Any] | None,
    execution_calibration: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build a factual decision surface using evidence known no later than the signal."""

    as_of = signal_posted_at.astimezone(UTC)
    market = market_context if isinstance(market_context, dict) else {}
    market_blob = market.get("market") if isinstance(market.get("market"), dict) else {}
    quote = (
        market_blob.get("quote_context")
        if isinstance(market_blob.get("quote_context"), dict)
        else {}
    )
    quote_time = _utc(quote.get("quote_time"))
    quote_future = quote_time is not None and quote_time > as_of
    mid = None if quote_future else _decimal(quote.get("mid"))
    low = _decimal(entry_low)
    high = _decimal(entry_high)
    if low is not None and high is not None and low > high:
        low, high = high, low

    zone_relation = "unknown"
    distance_to_zone: Decimal | None = None
    if mid is not None and low is not None and high is not None:
        if low <= mid <= high:
            zone_relation = "inside_entry_zone"
            distance_to_zone = Decimal("0")
        elif mid < low:
            zone_relation = "below_entry_zone"
            distance_to_zone = low - mid
        else:
            zone_relation = "above_entry_zone"
            distance_to_zone = mid - high

    direction = str(side or "").upper()
    targets = [value for value in (_decimal(x) for x in take_profits) if value is not None]
    targets_crossed = 0
    if mid is not None:
        if direction == "BUY":
            targets_crossed = sum(1 for target in targets if mid >= target)
        elif direction == "SELL":
            targets_crossed = sum(1 for target in targets if mid <= target)

    stop = _decimal(stop_loss)
    entry_mid = (
        (low + high) / Decimal("2")
        if low is not None and high is not None
        else (low if low is not None else high)
    )
    stop_distance = (
        abs(entry_mid - stop) if entry_mid is not None and stop is not None else None
    )
    distance_ratio = (
        distance_to_zone / stop_distance
        if distance_to_zone is not None and stop_distance not in (None, Decimal("0"))
        else None
    )

    events = market.get("todays_scheduled_events")
    event_list = events if isinstance(events, list) else []
    nearest_event = _nearest_scheduled_event(event_list, as_of=as_of)

    return {
        "contract_version": CONTEXT_VERSION,
        "as_of_utc": as_of.isoformat(),
        "event": {
            "event_timing": str(market.get("event_timing") or "unknown"),
            "nearest_scheduled_event": nearest_event,
            "day_map_available": isinstance(events, list),
            "scheduled_event_count": len(event_list),
            "directional_prediction_allowed": False,
            "realized_event_outcome_available": False,
        },
        "liquidity": {
            "session": market.get("session"),
            "quote_state": (
                "invalid_future_quote"
                if quote_future
                else str(market.get("quote_state") or quote.get("quote_state") or "unknown")
            ),
            "quote_freshness": str(market.get("quote_freshness") or "unknown"),
            "quote_time_utc": quote_time.isoformat() if quote_time else None,
            "quote_age_seconds": quote.get("quote_age_seconds"),
            "mid": str(mid) if mid is not None else None,
            "spread": (
                str(quote.get("spread")) if quote.get("spread") is not None else None
            ),
        },
        "execution_geometry": {
            "entry_zone_relation_to_mid": zone_relation,
            "distance_mid_to_entry_zone_points": (
                str(distance_to_zone) if distance_to_zone is not None else None
            ),
            "entry_stop_distance_points": (
                str(stop_distance) if stop_distance is not None else None
            ),
            "distance_to_zone_over_stop": (
                str(distance_ratio) if distance_ratio is not None else None
            ),
            "targets_already_crossed_at_quote": targets_crossed,
            "target_count": len(targets),
        },
        "broker_execution_calibration": _execution_calibration(
            execution_calibration, as_of=as_of
        ),
        "research_only": True,
        "live_money_execution_allowed": False,
    }


__all__ = [
    "CONTEXT_VERSION",
    "build_event_liquidity_execution_context",
]
