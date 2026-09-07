"""Day 11 full-history fidelity closure over the frozen 82-trade corpus.

This runner applies only the diagnosed Day 11 paper-reconciliation fidelity corrections.
It does not alter reconciliation tolerances, sample floors, provider/member routing, sizing,
or live-money authority. The paper side is reconstructed exclusively from the isolated
retrospective Twelve M1 calibration path and is completed before broker truth is compared.

The corpus is frozen at the completion timestamp of the original Day 11 baseline run and is
limited to the same five source IDs that existed in that run. Eligibility uses only signal shape,
closed-position cleanliness, broker-evidence existence, and paper/broker leg cardinality; no
realised broker outcome value is used to select a signal.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from statistics import median
from uuid import UUID

from sqlalchemy import text

from app.aidy_market_client import AidyMarketClient
from app.db import get_engine, get_session_factory
from app.provider_day11_calibration_replay import replay_signal_calibration
from app.provider_day11_replay import _evidence_digest, _percentile_cont, _persist_sample
from app.provider_execution_calibration import (
    CALIBRATION_TOLERANCE_VERSION,
    DEFAULT_RECONCILIATION_TOLERANCE,
    intelligence_mode_for_calibration,
    reconciliation_status,
)

DAY11_BASELINE_RUN_ID = UUID("c5fae236-a63f-4340-99e7-7890e08f782a")
DAY11_WIDENED_SIGNAL_COUNT = 82
DAY11_PROVIDER_COUNT = 5


def _code_sha() -> str:
    return (os.getenv("RENDER_GIT_COMMIT") or os.getenv("GIT_COMMIT") or "unknown")[:40]


def _frozen_cutoff(session) -> datetime:
    value = session.execute(
        text(
            """
            SELECT completed_at
            FROM provider_execution_reconciliation_runs
            WHERE id=:baseline_run_id
            """
        ),
        {"baseline_run_id": DAY11_BASELINE_RUN_ID},
    ).scalar_one()
    if not isinstance(value, datetime):
        raise RuntimeError("day11_baseline_completion_missing")
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _frozen_clean_candidates(session) -> dict[UUID, list[dict]]:
    """Return all clean historical trades for the original five, frozen at baseline cutoff."""
    rows = [
        dict(row)
        for row in session.execute(
            text(
                """
                WITH baseline_sources AS (
                    SELECT DISTINCT source_id
                    FROM provider_execution_reconciliation_samples
                    WHERE run_id=:baseline_run_id
                ), cutoff AS (
                    SELECT completed_at AS frozen_at
                    FROM provider_execution_reconciliation_runs
                    WHERE id=:baseline_run_id
                ), eligible AS (
                    SELECT
                        s.id,s.source_id,s.source_message_id,s.symbol,s.side,s.order_type,
                        s.entry_low,s.entry_high,s.stop_loss,s.take_profits,s.has_open_runner,
                        s.original_text,s.source_posted_at,
                        COALESCE(src.chat_title,src.source_alias) AS provider_title,
                        COUNT(p.id)::integer AS position_count,
                        (jsonb_array_length(COALESCE(s.take_profits,'[]'::jsonb))
                           + CASE WHEN s.has_open_runner THEN 1 ELSE 0 END)::integer AS paper_leg_count
                    FROM signals s
                    JOIN baseline_sources bs ON bs.source_id=s.source_id
                    JOIN sources src ON src.id=s.source_id
                    JOIN positions p ON p.signal_id=s.id
                    CROSS JOIN cutoff c
                    WHERE src.status='testing'
                      AND s.parser_status='accepted'
                      AND UPPER(COALESCE(s.symbol,''))='XAUUSD'
                      AND s.side IN ('BUY','SELL')
                      AND s.stop_loss IS NOT NULL
                      AND s.source_posted_at IS NOT NULL
                      AND s.source_posted_at <= c.frozen_at
                    GROUP BY s.id,s.source_id,src.id
                    HAVING BOOL_AND(p.status='closed')
                       AND BOOL_AND(p.close_reason IN ('external_close','provider_close','broker_settled'))
                       AND EXISTS (
                           SELECT 1 FROM broker_deals bd
                           WHERE bd.signal_id=s.id
                             AND UPPER(COALESCE(bd.symbol,''))='XAUUSD'
                       )
                )
                SELECT * FROM eligible
                WHERE position_count=paper_leg_count
                ORDER BY source_id,source_posted_at,id
                """
            ),
            {"baseline_run_id": DAY11_BASELINE_RUN_ID},
        ).mappings()
    ]
    grouped: dict[UUID, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["source_id"], []).append(row)
    return grouped


def _existing_run(session, *, code_sha: str):
    return session.execute(
        text(
            """
            SELECT id,status,completed_at,evidence_digest
            FROM provider_execution_reconciliation_runs
            WHERE tolerance_version=:version AND code_sha=:code_sha
              AND completed_at IS NOT NULL
            ORDER BY completed_at DESC,id DESC LIMIT 1
            """
        ),
        {"version": CALIBRATION_TOLERANCE_VERSION, "code_sha": code_sha},
    ).mappings().first()


async def run() -> dict:
    client = AidyMarketClient.from_environment()
    if client is None:
        raise RuntimeError("day11_aidy_market_credentials_missing")

    session_factory = get_session_factory()
    code_sha = _code_sha()
    tolerance = DEFAULT_RECONCILIATION_TOLERANCE
    now = datetime.now(UTC)

    lock_connection = get_engine().connect()
    locked = bool(
        lock_connection.execute(
            text("SELECT pg_try_advisory_lock(hashtext('provider_day11_widened_diagnostic_v1'))")
        ).scalar_one()
    )
    if not locked:
        lock_connection.close()
        return {
            "status": "SKIPPED_LOCK_HELD",
            "research_only": True,
            "live_money_execution_allowed": False,
        }

    try:
        with session_factory() as session:
            existing = _existing_run(session, code_sha=code_sha)
            if existing is not None:
                return {
                    "run_id": str(existing["id"]),
                    "status": str(existing["status"]),
                    "evidence_digest": existing["evidence_digest"],
                    "skipped_existing_code_sha": True,
                    "research_only": True,
                    "live_money_execution_allowed": False,
                }

            cutoff = _frozen_cutoff(session)
            candidates = _frozen_clean_candidates(session)
            selected_count = sum(len(rows) for rows in candidates.values())
            if len(candidates) != DAY11_PROVIDER_COUNT:
                raise RuntimeError(
                    f"day11_widened_provider_count_invalid:{len(candidates)}"
                )
            if selected_count != DAY11_WIDENED_SIGNAL_COUNT:
                raise RuntimeError(
                    f"day11_widened_signal_count_invalid:{selected_count}"
                )

            run_id = session.execute(
                text(
                    """
                    INSERT INTO provider_execution_reconciliation_runs(tolerance_version,code_sha)
                    VALUES (:version,:code_sha) RETURNING id
                    """
                ),
                {"version": CALIBRATION_TOLERANCE_VERSION, "code_sha": code_sha},
            ).scalar_one()
            session.commit()

            try:
                attempted: list[dict] = []
                for source_id, signals in candidates.items():
                    for signal in signals:
                        try:
                            result = await replay_signal_calibration(
                                session=session,
                                client=client,
                                signal=signal,
                                now=now,
                            )
                        except Exception as exc:
                            posted = signal["source_posted_at"].astimezone(UTC)
                            result = {
                                "replay_from": posted.replace(second=0, microsecond=0),
                                "replay_to": posted.replace(second=0, microsecond=0)
                                + timedelta(minutes=1),
                                "broker_deal_count": 0,
                                "exclusion_reason": (
                                    f"replay_error:{type(exc).__name__}:{exc}"
                                )[:160],
                            }
                        _persist_sample(session, run_id=run_id, signal=signal, result=result)
                        attempted.append(
                            {"source_id": source_id, "signal_id": signal["id"], **result}
                        )
                        session.commit()

                comparable = [row for row in attempted if row.get("abs_r_delta") is not None]
                total_comparable = len(comparable)
                summaries: list[dict] = []
                for source_id in sorted(candidates, key=str):
                    rows = [row for row in comparable if row["source_id"] == source_id]
                    deltas = [Decimal(str(row["abs_r_delta"])) for row in rows]
                    med = Decimal(str(median(deltas))) if deltas else Decimal("0")
                    p95 = _percentile_cont(deltas, Decimal("0.95")) if deltas else Decimal("0")
                    lifecycle_rate = (
                        Decimal(sum(bool(row.get("lifecycle_matches")) for row in rows))
                        / Decimal(len(rows))
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
                                :run_id,:source_id,:sample_count,:median,:p95,:lifecycle,:status,:mode
                            )
                            """
                        ),
                        {
                            "run_id": run_id,
                            "source_id": source_id,
                            "sample_count": len(rows),
                            "median": med,
                            "p95": p95,
                            "lifecycle": lifecycle_rate,
                            "status": status,
                            "mode": mode,
                        },
                    )
                    summaries.append(
                        {
                            "source_id": str(source_id),
                            "attempted": len(candidates[source_id]),
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
                    if len(summaries) == DAY11_PROVIDER_COUNT
                    and all(row["status"] == "RECONCILED" for row in summaries)
                    else "WAITING_RECONCILIATION"
                )
                digest = _evidence_digest(attempted)
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
                        "provider_count": len(summaries),
                        "attempted": len(attempted),
                        "comparable": total_comparable,
                        "digest": digest,
                    },
                )
                session.commit()
                exclusions: dict[str, dict[str, int]] = {}
                for row in attempted:
                    reason = row.get("exclusion_reason")
                    if reason is None:
                        continue
                    source_key = str(row["source_id"])
                    bucket = exclusions.setdefault(source_key, {})
                    bucket[str(reason)] = bucket.get(str(reason), 0) + 1

                summary = {
                    "run_id": str(run_id),
                    "status": run_status,
                    "fidelity_closure": True,
                    "replay_mechanics_changed": True,
                    "fidelity_mode": "canonical_allocation_keyed_leg_m1",
                    "tolerance_version": CALIBRATION_TOLERANCE_VERSION,
                    "baseline_run_id": str(DAY11_BASELINE_RUN_ID),
                    "frozen_cutoff_utc": cutoff.isoformat(),
                    "corpus_size": DAY11_WIDENED_SIGNAL_COUNT,
                    "calibration_source_kind": "calibration_backfill",
                    "calibration_source_provider": "twelve_data",
                    "attempted": len(attempted),
                    "comparable": total_comparable,
                    "evidence_digest": digest,
                    "providers": summaries,
                    "exclusions": exclusions,
                    "research_only": True,
                    "live_money_execution_allowed": False,
                }
                print(
                    "PROVIDER_DAY11_FIDELITY_CLOSURE="
                    + json.dumps(summary, sort_keys=True),
                    flush=True,
                )
                return summary
            except Exception as exc:
                session.execute(
                    text(
                        """
                        UPDATE provider_execution_reconciliation_runs
                        SET completed_at=now(),status='FAILED',failure_reason=:reason
                        WHERE id=:run_id
                        """
                    ),
                    {"run_id": run_id, "reason": f"{type(exc).__name__}:{exc}"[:160]},
                )
                session.commit()
                raise
    finally:
        try:
            lock_connection.execute(
                text(
                    "SELECT pg_advisory_unlock(hashtext('provider_day11_widened_diagnostic_v1'))"
                )
            )
        finally:
            lock_connection.close()


if __name__ == "__main__":
    asyncio.run(run())
