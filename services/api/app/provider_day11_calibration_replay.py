"""Calibration-only Day 11 paper replay using retrospective Twelve Data M1 evidence.

This module exists solely to resolve the frozen Day 11 paper-vs-broker reconciliation.
It never calls the normal PIT AIDY market path. The AIDY client must prove every returned
bar is source_kind=calibration_backfill, source_provider=twelve_data, pit_eligible=false,
research_only=true and live_money_execution_allowed=false before replay can proceed.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from app.aidy_market_client import AidyMarketClient
from app.aidy_shadow_resolver import _initial_state, lifecycle_watermark, replay_bars
from app.layer_allocation import allocate_entry_targets
from app.provider_day11_replay import (
    REPLAY_HORIZON,
    _broker_leg_truth,
    _geometry_for_entry,
    _load_lifecycle_events,
    _minute_floor,
    _paper_lifecycle,
    _research_entries,
    _targets,
)

CALIBRATION_EVIDENCE_DOMAIN = "calibration_backfill:twelve_data"


def _leg_id(signal_id: object, entry_index: int, tp_index: int):
    return uuid5(NAMESPACE_URL, f"super-signals:day11:{signal_id}:{entry_index}:{tp_index}")


async def replay_signal_calibration(
    *, session: Any, client: AidyMarketClient, signal: dict[str, Any], now: datetime
) -> dict[str, Any]:
    posted = signal["source_posted_at"]
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=UTC)
    else:
        posted = posted.astimezone(UTC)
    replay_from = _minute_floor(posted)
    replay_to = _minute_floor(min(posted + REPLAY_HORIZON, now))
    if replay_to <= replay_from:
        return {"replay_from": replay_from, "replay_to": replay_from + timedelta(minutes=1), "exclusion_reason": "replay_window_empty"}

    broker_legs, broker_lifecycle, broker_deals, broker_error = _broker_leg_truth(session, signal)
    if broker_error is not None or broker_legs is None or broker_lifecycle is None:
        return {"replay_from": replay_from, "replay_to": replay_to, "broker_deal_count": broker_deals, "exclusion_reason": broker_error or "broker_leg_truth_unavailable"}

    entries = _research_entries(signal)
    targets = _targets(signal)
    all_targets: list[Decimal | None] = list(targets)
    if bool(signal.get("has_open_runner")):
        all_targets.append(None)
    if not entries or not all_targets:
        return {"replay_from": replay_from, "replay_to": replay_to, "broker_deal_count": broker_deals, "exclusion_reason": "paper_geometry_unavailable"}
    try:
        allocations = allocate_entry_targets(entries, tuple(all_targets))
    except ValueError as exc:
        return {"replay_from": replay_from, "replay_to": replay_to, "broker_deal_count": broker_deals, "exclusion_reason": f"paper_allocation_invalid:{exc}"[:160]}

    window = await client.fetch_calibration_m1(window_id=str(signal["id"]), start=replay_from, end=replay_to)
    if not window.complete:
        return {"replay_from": replay_from, "replay_to": replay_to, "broker_deal_count": broker_deals, "exclusion_reason": ("calibration_m1_incomplete:" + window.missing_open_times[0].isoformat())[:160]}
    bars = list(window.bars)
    events = _load_lifecycle_events(session, signal["id"])
    event_mark = lifecycle_watermark(events)
    sibling_entries = [(int(entry.entry_index), (entry.low + entry.high) / Decimal("2")) for entry in entries]
    by_entry: dict[int, list[Any]] = {}
    for allocation in allocations:
        by_entry.setdefault(int(allocation.entry.entry_index), []).append(allocation)

    state_rows: list[tuple[int, Any]] = []
    for entry in entries:
        entry_index = int(entry.entry_index)
        assigned = tuple(by_entry.get(entry_index, []))
        if not assigned:
            continue
        geometry = _geometry_for_entry(
            signal=signal,
            entry=entry,
            targets=targets,
            leg_plan=tuple((item.tp_index, item.take_profit) for item in assigned),
        )
        leg_ids = {item.tp_index: _leg_id(signal["id"], entry_index, item.tp_index) for item in assigned}
        state = _initial_state(geometry=geometry, leg_ids=leg_ids, signal_posted_at=posted, lifecycle_mark=lifecycle_watermark([]))
        state = replay_bars(state=state, geometry=geometry, events=events, bars=bars, signal_posted_at=posted, sibling_entries=sibling_entries, full_replay=True)
        state_rows.append((entry_index, state))

    states = [state for _, state in state_rows]
    if any(not state.terminal for state in states):
        return {"replay_from": replay_from, "replay_to": replay_to, "broker_deal_count": broker_deals, "exclusion_reason": "paper_outcome_unresolved_48h"}
    if any(state.score_block_reason is not None for state in states):
        blocked = next(state.score_block_reason for state in states if state.score_block_reason)
        return {"replay_from": replay_from, "replay_to": replay_to, "broker_deal_count": broker_deals, "exclusion_reason": f"paper_score_blocked:{blocked}"[:160]}

    paper_legs: dict[tuple[int, int], Decimal] = {}
    for entry_index, state in state_rows:
        for leg in state.legs:
            if leg.status != "closed":
                continue
            key = (entry_index, int(leg.tp_index))
            if key in paper_legs:
                return {"replay_from": replay_from, "replay_to": replay_to, "broker_deal_count": broker_deals, "exclusion_reason": "paper_leg_key_duplicate"}
            paper_legs[key] = leg.realized_r

    if set(paper_legs) != set(broker_legs):
        return {"replay_from": replay_from, "replay_to": replay_to, "broker_deal_count": broker_deals, "exclusion_reason": "paper_broker_leg_key_mismatch"}

    leg_deltas = {key: abs(paper_legs[key] - broker_legs[key]) for key in paper_legs}
    paper_r = sum(paper_legs.values(), Decimal("0"))
    broker_r = sum(broker_legs.values(), Decimal("0"))
    abs_r_delta = max(leg_deltas.values(), default=Decimal("0"))
    paper_lifecycle = _paper_lifecycle(states)
    digests = sorted(str(state.evidence_digest or "") for state in states)
    aidy_digest = hashlib.sha256((CALIBRATION_EVIDENCE_DOMAIN + "|" + event_mark + "|" + "|".join(digests)).encode()).hexdigest()
    return {
        "replay_from": replay_from,
        "replay_to": replay_to,
        "paper_r": paper_r,
        "broker_r": broker_r,
        "abs_r_delta": abs_r_delta,
        "paper_lifecycle": paper_lifecycle,
        "broker_lifecycle": broker_lifecycle,
        "lifecycle_matches": paper_lifecycle == broker_lifecycle,
        "aidy_evidence_digest": aidy_digest,
        "broker_deal_count": broker_deals,
        "exclusion_reason": None,
    }
