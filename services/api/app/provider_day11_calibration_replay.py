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
from uuid import uuid4

from app.aidy_market_client import AidyMarketClient
from app.aidy_shadow_resolver import _initial_state, lifecycle_watermark, replay_bars
from app.provider_day11_replay import (
    REPLAY_HORIZON,
    _broker_truth,
    _geometry_for_entry,
    _load_lifecycle_events,
    _minute_floor,
    _paper_lifecycle,
    _research_entries,
    _targets,
)

CALIBRATION_EVIDENCE_DOMAIN = "calibration_backfill:twelve_data"


async def replay_signal_calibration(
    *,
    session: Any,
    client: AidyMarketClient,
    signal: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    """Replay one frozen real signal against calibration-only historical M1."""
    posted = signal["source_posted_at"]
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=UTC)
    else:
        posted = posted.astimezone(UTC)
    replay_from = _minute_floor(posted)
    replay_to = _minute_floor(min(posted + REPLAY_HORIZON, now))
    if replay_to <= replay_from:
        return {
            "replay_from": replay_from,
            "replay_to": replay_from + timedelta(minutes=1),
            "exclusion_reason": "replay_window_empty",
        }

    broker_r, broker_lifecycle, broker_deals, broker_error = _broker_truth(session, signal)
    if broker_error is not None:
        return {
            "replay_from": replay_from,
            "replay_to": replay_to,
            "broker_deal_count": broker_deals,
            "exclusion_reason": broker_error,
        }

    entries = _research_entries(signal)
    targets = _targets(signal)
    if not entries or (not targets and not bool(signal.get("has_open_runner"))):
        return {
            "replay_from": replay_from,
            "replay_to": replay_to,
            "broker_deal_count": broker_deals,
            "exclusion_reason": "paper_geometry_unavailable",
        }

    window = await client.fetch_calibration_m1(
        window_id=str(signal["id"]),
        start=replay_from,
        end=replay_to,
    )
    if not window.complete:
        return {
            "replay_from": replay_from,
            "replay_to": replay_to,
            "broker_deal_count": broker_deals,
            "exclusion_reason": (
                "calibration_m1_incomplete:"
                + window.missing_open_times[0].isoformat()
            )[:160],
        }
    bars = list(window.bars)

    events = _load_lifecycle_events(session, signal["id"])
    event_mark = lifecycle_watermark(events)
    sibling_entries = [
        (int(entry.entry_index), (entry.low + entry.high) / Decimal("2")) for entry in entries
    ]

    states: list[Any] = []
    for entry in entries:
        geometry = _geometry_for_entry(signal=signal, entry=entry, targets=targets)
        leg_ids = {index: uuid4() for index in geometry.targets}
        state = _initial_state(
            geometry=geometry,
            leg_ids=leg_ids,
            signal_posted_at=posted,
            lifecycle_mark=lifecycle_watermark([]),
        )
        state = replay_bars(
            state=state,
            geometry=geometry,
            events=events,
            bars=bars,
            signal_posted_at=posted,
            sibling_entries=sibling_entries,
            full_replay=True,
        )
        states.append(state)

    if any(not state.terminal for state in states):
        return {
            "replay_from": replay_from,
            "replay_to": replay_to,
            "broker_deal_count": broker_deals,
            "exclusion_reason": "paper_outcome_unresolved_48h",
        }
    if any(state.score_block_reason is not None for state in states):
        blocked = next(state.score_block_reason for state in states if state.score_block_reason)
        return {
            "replay_from": replay_from,
            "replay_to": replay_to,
            "broker_deal_count": broker_deals,
            "exclusion_reason": f"paper_score_blocked:{blocked}"[:160],
        }

    paper_r = sum(
        (leg.realized_r for state in states for leg in state.legs if leg.status == "closed"),
        Decimal("0"),
    )
    paper_lifecycle = _paper_lifecycle(states)
    digests = sorted(str(state.evidence_digest or "") for state in states)
    aidy_digest = hashlib.sha256(
        (
            CALIBRATION_EVIDENCE_DOMAIN
            + "|"
            + event_mark
            + "|"
            + "|".join(digests)
        ).encode()
    ).hexdigest()
    assert broker_r is not None and broker_lifecycle is not None
    return {
        "replay_from": replay_from,
        "replay_to": replay_to,
        "paper_r": paper_r,
        "broker_r": broker_r,
        "abs_r_delta": abs(paper_r - broker_r),
        "paper_lifecycle": paper_lifecycle,
        "broker_lifecycle": broker_lifecycle,
        "lifecycle_matches": paper_lifecycle == broker_lifecycle,
        "aidy_evidence_digest": aidy_digest,
        "broker_deal_count": broker_deals,
        "exclusion_reason": None,
    }
