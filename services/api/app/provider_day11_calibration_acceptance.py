"""Production Day 11 acceptance using only the frozen 56-signal calibration corpus.

The baseline signal set is immutable: it is read from the original failed production run
c5fae236-a63f-4340-99e7-7890e08f782a. Paper replay uses only AIDY's separate retrospective
Twelve calibration route. Broker truth remains real broker_deals. Tolerances are the same
provider_day11_v1 values already persisted in production. This module is research-only and
cannot mutate broker positions, member risk, provider routing/status, sizing or AIDY authority.
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
DAY11_FROZEN_SIGNAL_COUNT = 56


def _code_sha() -> str:
    return (os.getenv("RENDER_GIT_COMMIT") or os.getenv("GIT_COMMIT") or "unknown")[:40]


def _baseline_count(session) -> int:
    return int(
        session.execute(
            text(
                """
                SELECT COUNT(DISTINCT signal_id)
                FROM provider_execution_reconciliation_samples
                WHERE run_id=:baseline_run_id
                """
            ),
            {"baseline_run_id": DAY11_BASELINE_RUN_ID},
        ).scalar_one()
    )


def _frozen_clean_candidates(session) -> dict[UUID, list[dict]]:
    """Return only the original 56 real broker-grounded Day 11 signals.

    Current broker/lifecycle completeness is revalidated, but no new signal may enter the
    corpus and no realised outcome is used to select among baseline signals.
    """
    rows = [
        dict(row)
        for row in session.execute(
            text(
                """
                WITH baseline AS (
                    SELECT DISTINCT signal_id
                    FROM provider_execution_reconciliation_samples
                    WHERE run_id=:baseline_run_id
                ), eligible AS (
                    SELECT
                        s.id,s.source_id,s.source_message_id,s.symbol,s.side,s.order_type,
                        s.entry_low,s.entry_high,s.stop_loss,s.take_profits,s.has_open_runner,
                        s.original_text,s.source_posted_at,
                        COALESCE(src.chat_title,src.source_alias) AS provider_title,
                        COUNT(p.id)::integer AS position_count,
                        (jsonb_array_length(COALESCE(s.take_profits,'[]'::jsonb))
                           + CASE WHEN s.has_open_runner THEN 1 ELSE 0 END)::integer AS paper_leg_count
                    FROM baseline b
                    JOIN signals s ON s.id=b.signal_id
                    JOIN sources src ON src.id=s.source_id
                    JOIN positions p ON p.signal_id=s.id
                    WHERE src.status='testing'
                      AND s.parser_status='accepted'
                      AND UPPER(COALESCE(s.symbol,''))='XAUUSD'
                      AND s.side IN ('BUY','SELL')
                      AND s.stop_loss IS NOT NULL
                      AND s.source_posted_at IS NOT NULL
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
                ORDER BY source_id,source_posted_at DESC,id DESC
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
            text("SELECT pg_try_advisory_lock(hashtext('provider_day11_calibration_acceptance_v1'))")
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

            baseline_count = _baseline_count(session)
            if baseline_count != DAY11_FROZEN_SIGNAL_COUNT:
                raise RuntimeError(
                    f"day11_frozen_baseline_count_invalid:{baseline_count}"
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
                candidates = _frozen_clean_candidates(session)
                selected_count = sum(len(rows) for rows in candidates.values())
                if len(candidates) != 5:
                    raise RuntimeError(
                        f"day11_established_provider_count_invalid:{len(candidates)}"
                    )
                if selected_count != DAY11_FROZEN_SIGNAL_COUNT:
                    raise RuntimeError(
                        f"day11_frozen_clean_signal_count_invalid:{selected_count}"
                    )
                insufficient = {
                    str(source_id): len(rows)
                    for source_id, rows in candidates.items()
                    if len(rows) < tolerance.min_provider_signals
                }
                if insufficient:
                    raise RuntimeError(
                        f"day11_clean_provider_samples_insufficient:{insufficient}"
                    )

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
                    if len(summaries) == 5
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
                summary = {
                    "run_id": str(run_id),
                    "status": run_status,
                    "tolerance_version": CALIBRATION_TOLERANCE_VERSION,
                    "baseline_run_id": str(DAY11_BASELINE_RUN_ID),
                    "calibration_source_kind": "calibration_backfill",
                    "calibration_source_provider": "twelve_data",
                    "attempted": len(attempted),
                    "comparable": total_comparable,
                    "evidence_digest": digest,
                    "providers": summaries,
                    "research_only": True,
                    "live_money_execution_allowed": False,
                }
                print(
                    "PROVIDER_DAY11_CALIBRATION_ACCEPTANCE="
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
                    "SELECT pg_advisory_unlock(hashtext('provider_day11_calibration_acceptance_v1'))"
                )
            )
        finally:
            lock_connection.close()


if __name__ == "__main__":
    asyncio.run(run())
