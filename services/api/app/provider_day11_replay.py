"""One-shot Day 11 paper-to-broker reconciliation for the established five.

This runner is deliberately research-only. It reads accepted provider signals, immutable
provider lifecycle events, AIDY's authenticated PIT M1 feed, and broker_deals. It writes
only the Day 11 reconciliation evidence tables created by migration 0060. It never calls
MetaAPI and never mutates positions, member risk, provider status, or AIDY authority.

Usage from the API runtime after migrations:
    python -m app.provider_day11_replay
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from statistics import median
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from app.aidy_market_client import AidyM1Bar, AidyMarketClient
from app.aidy_shadow_resolver import (
    OriginalGeometry,
    _initial_state,
    lifecycle_watermark,
    replay_bars,
)
from app.db import get_session_factory
from app.provider_execution_calibration import (
    CALIBRATION_TOLERANCE_VERSION,
    DEFAULT_RECONCILIATION_TOLERANCE,
    intelligence_mode_for_calibration,
    reconciliation_status,
)
from app.shadow_trading_v2 import ShadowTradeService, _decimal

MAX_SIGNALS_PER_PROVIDER = 15
REPLAY_HORIZON = timedelta(hours=48)
FETCH_CHUNK = timedelta(hours=24)
CLEAN_BROKER_CLOSE_REASONS = ("external_close", "provider_close", "broker_settled")


def _minute_floor(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("day11_timestamp_timezone_required")
    return value.astimezone(UTC).replace(second=0, microsecond=0)


def _decimal_required(value: object, *, field: str) -> Decimal:
    result = _decimal(value)
    if result is None or not result.is_finite():
        raise ValueError(f"{field}_invalid")
    return result


def _percentile_cont(values: list[Decimal], quantile: Decimal) -> Decimal:
    if not values:
        return Decimal("0")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = quantile * Decimal(len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - Decimal(lower)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _research_entries(signal: dict[str, Any]) -> tuple[Any, ...]:
    # Reuse the exact Provider Lab entry/layer parser without creating a shadow row.
    return ShadowTradeService._research_entries(signal)


def _targets(signal: dict[str, Any]) -> tuple[Decimal, ...]:
    raw = signal.get("take_profits") or []
    if not isinstance(raw, list):
        return ()
    values: list[Decimal] = []
    for item in raw:
        parsed = _decimal(item)
        if parsed is not None and parsed > 0:
            values.append(parsed)
    return tuple(values)


def _geometry_for_entry(
    *, signal: dict[str, Any], entry: Any, targets: tuple[Decimal, ...]
) -> OriginalGeometry:
    stop = _decimal_required(signal.get("stop_loss"), field="stop_loss")
    legs: list[dict[str, Any]] = [
        {"tp_index": index, "target_price": str(target), "is_runner": False}
        for index, target in enumerate(targets, start=1)
    ]
    if bool(signal.get("has_open_runner")):
        legs.append({"tp_index": len(legs) + 1, "target_price": None, "is_runner": True})
    payload = {
        "side": str(signal["side"]),
        "entry_order_type": str(entry.order_type),
        "entry_low": str(entry.low),
        "entry_high": str(entry.high),
        "initial_stop": str(stop),
        "entry_index": int(entry.entry_index),
        "legs": legs,
    }
    return OriginalGeometry.from_payload(payload)


async def _fetch_bars(
    client: AidyMarketClient, *, start: datetime, end: datetime
) -> tuple[list[AidyM1Bar], str | None]:
    bars: list[AidyM1Bar] = []
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + FETCH_CHUNK, end)
        window = await client.fetch_m1(start=cursor, end=chunk_end)
        if not window.complete:
            return [], f"aidy_m1_incomplete:{window.missing_open_times[0].isoformat()}"
        bars.extend(window.bars)
        cursor = chunk_end
    bars.sort(key=lambda item: item.open_time_utc)
    return bars, None


def _load_lifecycle_events(session: Any, signal_id: UUID) -> list[dict[str, Any]]:
    return [
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
            {"signal_id": signal_id},
        ).mappings()
    ]


def _paper_lifecycle(states: list[Any]) -> str:
    reasons = {
        str(reason).lower()
        for state in states
        for reason in [state.close_reason]
        if reason is not None
    }
    leg_reasons = {
        str(leg.exit_reason).lower()
        for state in states
        for leg in state.legs
        if leg.exit_reason is not None
    }
    combined = reasons | leg_reasons
    if any("provider" in value or "manual" in value or "partial" in value for value in combined):
        return "provider_managed"
    if any("stop" in value for value in combined):
        return "stop_loss"
    realized = [leg for state in states for leg in state.legs if leg.status == "closed"]
    if realized and all(str(leg.exit_reason).lower() == "target" for leg in realized):
        return "profit_target"
    return "paper_other"


def _broker_truth(session: Any, signal: dict[str, Any]) -> tuple[Decimal | None, str | None, int, str | None]:
    rows = list(
        session.execute(
            text(
                """
                WITH deals AS (
                    SELECT
                        position_id,
                        SUM(price*volume) FILTER (
                            WHERE entry_type='DEAL_ENTRY_IN' AND price IS NOT NULL AND volume>0
                        ) / NULLIF(SUM(volume) FILTER (
                            WHERE entry_type='DEAL_ENTRY_IN' AND price IS NOT NULL AND volume>0
                        ),0) AS entry_vwap,
                        SUM(price*volume) FILTER (
                            WHERE entry_type='DEAL_ENTRY_OUT' AND price IS NOT NULL AND volume>0
                        ) / NULLIF(SUM(volume) FILTER (
                            WHERE entry_type='DEAL_ENTRY_OUT' AND price IS NOT NULL AND volume>0
                        ),0) AS exit_vwap,
                        COUNT(*) AS deal_count
                    FROM broker_deals
                    WHERE signal_id=:signal_id
                      AND UPPER(COALESCE(symbol,''))='XAUUSD'
                    GROUP BY position_id
                )
                SELECT p.id,p.close_reason,p.take_profit,p.status,d.entry_vwap,d.exit_vwap,d.deal_count
                FROM positions p
                LEFT JOIN deals d ON d.position_id=p.id
                WHERE p.signal_id=:signal_id
                ORDER BY p.entry_index,p.tp_index,p.id
                """
            ),
            {"signal_id": signal["id"]},
        ).mappings()
    )
    if not rows:
        return None, None, 0, "broker_positions_missing"
    if any(str(row["status"]) != "closed" for row in rows):
        return None, None, 0, "broker_position_not_closed"
    if any(str(row["close_reason"]) not in CLEAN_BROKER_CLOSE_REASONS for row in rows):
        return None, None, 0, "broker_noncanonical_close_reason"
    if any(row["entry_vwap"] is None or row["exit_vwap"] is None for row in rows):
        return None, None, sum(int(row["deal_count"] or 0) for row in rows), "broker_vwap_incomplete"

    stop = _decimal_required(signal.get("stop_loss"), field="broker_initial_stop")
    side = str(signal["side"]).upper()
    direction = Decimal("1") if side == "BUY" else Decimal("-1")
    total_r = Decimal("0")
    position_classes: list[str] = []
    total_deals = 0
    for row in rows:
        entry = _decimal_required(row["entry_vwap"], field="broker_entry")
        exit_price = _decimal_required(row["exit_vwap"], field="broker_exit")
        risk = abs(entry - stop)
        if risk <= 0:
            return None, None, total_deals, "broker_risk_distance_invalid"
        total_r += ((exit_price - entry) * direction) / risk
        total_deals += int(row["deal_count"] or 0)
        target = _decimal(row["take_profit"])
        if str(row["close_reason"]) == "provider_close":
            position_classes.append("provider_managed")
        elif target is not None and abs(exit_price - target) <= Decimal("0.75"):
            position_classes.append("profit_target")
        elif abs(exit_price - stop) <= Decimal("0.75"):
            position_classes.append("stop_loss")
        else:
            position_classes.append("broker_external")

    if "provider_managed" in position_classes:
        lifecycle = "provider_managed"
    elif "stop_loss" in position_classes:
        lifecycle = "stop_loss"
    elif position_classes and all(value == "profit_target" for value in position_classes):
        lifecycle = "profit_target"
    else:
        lifecycle = "broker_external"
    return total_r, lifecycle, total_deals, None


def _candidate_signals(session: Any) -> dict[UUID, list[dict[str, Any]]]:
    rows = [
        dict(row)
        for row in session.execute(
            text(
                """
                SELECT
                    s.id,s.source_id,s.source_message_id,s.symbol,s.side,s.order_type,
                    s.entry_low,s.entry_high,s.stop_loss,s.take_profits,s.has_open_runner,
                    s.original_text,s.source_posted_at,
                    COALESCE(src.chat_title,src.source_alias) AS provider_title
                FROM signals s
                JOIN sources src ON src.id=s.source_id
                WHERE src.status='testing'
                  AND s.parser_status='accepted'
                  AND UPPER(COALESCE(s.symbol,''))='XAUUSD'
                  AND s.side IN ('BUY','SELL')
                  AND s.stop_loss IS NOT NULL
                  AND s.source_posted_at IS NOT NULL
                  AND EXISTS (SELECT 1 FROM positions p WHERE p.signal_id=s.id)
                ORDER BY src.id,s.source_posted_at DESC,s.id DESC
                """
            )
        ).mappings()
    ]
    grouped: dict[UUID, list[dict[str, Any]]] = {}
    for row in rows:
        bucket = grouped.setdefault(row["source_id"], [])
        if len(bucket) < MAX_SIGNALS_PER_PROVIDER:
            # Broker cleanliness is checked again before replay. Keeping the candidate
            # query simple avoids using broker outcomes to influence paper mechanics.
            bucket.append(row)
    return grouped


async def _replay_signal(
    *,
    session: Any,
    client: AidyMarketClient,
    signal: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
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

    bars, market_error = await _fetch_bars(client, start=replay_from, end=replay_to)
    if market_error is not None:
        return {
            "replay_from": replay_from,
            "replay_to": replay_to,
            "broker_deal_count": broker_deals,
            "exclusion_reason": market_error,
        }
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
    aidy_digest = hashlib.sha256((event_mark + "|" + "|".join(digests)).encode()).hexdigest()
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


def _persist_sample(session: Any, *, run_id: UUID, signal: dict[str, Any], result: dict[str, Any]) -> None:
    session.execute(
        text(
            """
            INSERT INTO provider_execution_reconciliation_samples(
                run_id,source_id,signal_id,signal_posted_at,paper_r,broker_r,abs_r_delta,
                paper_lifecycle,broker_lifecycle,lifecycle_matches,replay_from,replay_to,
                aidy_evidence_digest,broker_deal_count,exclusion_reason
            ) VALUES (
                :run_id,:source_id,:signal_id,:signal_posted_at,:paper_r,:broker_r,:abs_r_delta,
                :paper_lifecycle,:broker_lifecycle,:lifecycle_matches,:replay_from,:replay_to,
                :aidy_evidence_digest,:broker_deal_count,:exclusion_reason
            )
            """
        ),
        {
            "run_id": run_id,
            "source_id": signal["source_id"],
            "signal_id": signal["id"],
            "signal_posted_at": signal["source_posted_at"],
            "paper_r": result.get("paper_r"),
            "broker_r": result.get("broker_r"),
            "abs_r_delta": result.get("abs_r_delta"),
            "paper_lifecycle": result.get("paper_lifecycle"),
            "broker_lifecycle": result.get("broker_lifecycle"),
            "lifecycle_matches": result.get("lifecycle_matches"),
            "replay_from": result["replay_from"],
            "replay_to": result["replay_to"],
            "aidy_evidence_digest": result.get("aidy_evidence_digest"),
            "broker_deal_count": int(result.get("broker_deal_count") or 0),
            "exclusion_reason": result.get("exclusion_reason"),
        },
    )


def _evidence_digest(rows: list[dict[str, Any]]) -> str:
    payload = [
        {
            "source_id": str(row["source_id"]),
            "signal_id": str(row["signal_id"]),
            "paper_r": str(row.get("paper_r")) if row.get("paper_r") is not None else None,
            "broker_r": str(row.get("broker_r")) if row.get("broker_r") is not None else None,
            "abs_r_delta": str(row.get("abs_r_delta")) if row.get("abs_r_delta") is not None else None,
            "paper_lifecycle": row.get("paper_lifecycle"),
            "broker_lifecycle": row.get("broker_lifecycle"),
            "lifecycle_matches": row.get("lifecycle_matches"),
            "exclusion_reason": row.get("exclusion_reason"),
            "aidy_evidence_digest": row.get("aidy_evidence_digest"),
        }
        for row in sorted(rows, key=lambda item: (str(item["source_id"]), str(item["signal_id"])))
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def run() -> dict[str, Any]:
    client = AidyMarketClient.from_environment()
    if client is None:
        raise RuntimeError("day11_aidy_market_credentials_missing")

    session_factory = get_session_factory()
    code_sha = (os.getenv("RENDER_GIT_COMMIT") or os.getenv("GIT_COMMIT") or "unknown")[:40]
    now = datetime.now(UTC)
    tolerance = DEFAULT_RECONCILIATION_TOLERANCE

    with session_factory() as session:
        # Fail immediately if migration 0060 is not present.
        session.execute(text("SELECT 1 FROM provider_execution_reconciliation_tolerances LIMIT 1"))
        run_id = session.execute(
            text(
                """
                INSERT INTO provider_execution_reconciliation_runs(tolerance_version,code_sha)
                VALUES (:version,:code_sha)
                RETURNING id
                """
            ),
            {"version": CALIBRATION_TOLERANCE_VERSION, "code_sha": code_sha},
        ).scalar_one()
        session.commit()

    attempted_rows: list[dict[str, Any]] = []
    try:
        with session_factory() as session:
            grouped = _candidate_signals(session)
            if len(grouped) != 5:
                raise RuntimeError(f"day11_established_provider_count_invalid:{len(grouped)}")

            for source_id, signals in grouped.items():
                for signal in signals:
                    try:
                        result = await _replay_signal(
                            session=session,
                            client=client,
                            signal=signal,
                            now=now,
                        )
                    except Exception as exc:  # retain auditable exclusion; do not invent an outcome
                        posted = signal["source_posted_at"]
                        replay_from = _minute_floor(posted)
                        result = {
                            "replay_from": replay_from,
                            "replay_to": max(replay_from + timedelta(minutes=1), _minute_floor(min(posted + REPLAY_HORIZON, now))),
                            "broker_deal_count": 0,
                            "exclusion_reason": f"replay_error:{type(exc).__name__}:{str(exc)}"[:160],
                        }
                    _persist_sample(session, run_id=run_id, signal=signal, result=result)
                    attempted_rows.append(
                        {
                            "source_id": source_id,
                            "signal_id": signal["id"],
                            **result,
                        }
                    )
                    session.commit()

            comparable = [row for row in attempted_rows if row.get("abs_r_delta") is not None]
            total_comparable = len(comparable)
            provider_summaries: list[dict[str, Any]] = []
            for source_id in sorted(grouped, key=str):
                rows = [row for row in comparable if row["source_id"] == source_id]
                deltas = [Decimal(str(row["abs_r_delta"])) for row in rows]
                med = Decimal(str(median(deltas))) if deltas else Decimal("0")
                p95 = _percentile_cont(deltas, Decimal("0.95")) if deltas else Decimal("0")
                lifecycle_rate = (
                    Decimal(sum(1 for row in rows if row.get("lifecycle_matches"))) / Decimal(len(rows))
                    if rows
                    else Decimal("0")
                )
                status = reconciliation_status(
                    provider_samples=len(rows),
                    total_samples=total_comparable,
                    median_abs_r_delta=med,
                    p95_abs_r_delta=p95,
                    lifecycle_agreement_rate=lifecycle_rate,
                    tolerance=tolerance,
                )
                mode = intelligence_mode_for_calibration(status)
                session.execute(
                    text(
                        """
                        INSERT INTO provider_execution_reconciliation_provider_results(
                            run_id,source_id,sample_count,median_abs_r_delta,p95_abs_r_delta,
                            lifecycle_agreement_rate,status,intelligence_mode
                        ) VALUES (
                            :run_id,:source_id,:sample_count,:median_abs_r_delta,:p95_abs_r_delta,
                            :lifecycle_agreement_rate,:status,:intelligence_mode
                        )
                        """
                    ),
                    {
                        "run_id": run_id,
                        "source_id": source_id,
                        "sample_count": len(rows),
                        "median_abs_r_delta": med,
                        "p95_abs_r_delta": p95,
                        "lifecycle_agreement_rate": lifecycle_rate,
                        "status": status,
                        "intelligence_mode": mode,
                    },
                )
                provider_summaries.append(
                    {
                        "source_id": str(source_id),
                        "sample_count": len(rows),
                        "median_abs_r_delta": str(med),
                        "p95_abs_r_delta": str(p95),
                        "lifecycle_agreement_rate": str(lifecycle_rate),
                        "status": status,
                        "intelligence_mode": mode,
                    }
                )

            run_status = (
                "RECONCILED"
                if len(provider_summaries) == 5
                and all(row["status"] == "RECONCILED" for row in provider_summaries)
                else "WAITING_RECONCILIATION"
            )
            digest = _evidence_digest(attempted_rows)
            session.execute(
                text(
                    """
                    UPDATE provider_execution_reconciliation_runs
                    SET completed_at=now(),status=:status,provider_count=:provider_count,
                        total_attempted_signals=:attempted,total_comparable_signals=:comparable,
                        evidence_digest=:digest
                    WHERE id=:run_id
                    """
                ),
                {
                    "run_id": run_id,
                    "status": run_status,
                    "provider_count": len(provider_summaries),
                    "attempted": len(attempted_rows),
                    "comparable": total_comparable,
                    "digest": digest,
                },
            )
            session.commit()
            summary = {
                "run_id": str(run_id),
                "status": run_status,
                "tolerance_version": CALIBRATION_TOLERANCE_VERSION,
                "attempted": len(attempted_rows),
                "comparable": total_comparable,
                "evidence_digest": digest,
                "providers": provider_summaries,
                "research_only": True,
                "live_money_execution_allowed": False,
            }
            print(json.dumps(summary, sort_keys=True))
            return summary
    except Exception as exc:
        with session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE provider_execution_reconciliation_runs
                    SET completed_at=now(),status='FAILED',failure_reason=:reason,
                        total_attempted_signals=:attempted
                    WHERE id=:run_id
                    """
                ),
                {
                    "run_id": run_id,
                    "reason": f"{type(exc).__name__}:{str(exc)}"[:160],
                    "attempted": len(attempted_rows),
                },
            )
            session.commit()
        raise


if __name__ == "__main__":
    asyncio.run(run())
