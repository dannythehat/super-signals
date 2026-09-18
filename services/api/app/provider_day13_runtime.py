"""Production compatibility runner for Provider Intelligence Day 13.

The Day 13 statistical contract lives in provider_day13_conditional. Production's
canonical attachment column is aidy_context_as_of_utc; this runner binds the
observation loader to that persisted schema without changing preregistration,
OOS, FDR, effect-size, shrinkage, provider population or authority semantics.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app import provider_day13_conditional as day13


def _load_observations(session: Any, *, cutoff: datetime) -> list[day13.ConditionalObservation]:
    rows = session.execute(
        text(
            """
            SELECT t.id AS trade_id,t.source_id,t.signal_id,t.side,t.session_bucket,
                   t.opened_at,t.closed_at,t.quality_r_multiple,
                   CASE t.session_bucket
                     WHEN 'asia' THEN 'asia'
                     WHEN 'london' THEN 'europe'
                     WHEN 'london_new_york_overlap' THEN 'ny_early'
                     WHEN 'new_york' THEN 'other'
                     WHEN 'rollover' THEN 'other'
                     ELSE t.session_bucket
                   END AS day13_session_bucket,
                   a.signal_posted_at,a.aidy_context_as_of_utc,a.provider_profile_effective_at,a.regime_json
            FROM shadow_trades t
            JOIN sources s ON s.id=t.source_id
            JOIN provider_signal_context_attachments a
              ON a.source_id=t.source_id AND a.signal_id=t.signal_id
            WHERE s.status='shadow'
              AND t.status='closed' AND t.closed_at IS NOT NULL AND t.closed_at<=:cutoff
              AND t.score_eligible
              AND t.provider_profile_pit_status='resolved'
              AND t.opened_at IS NOT NULL
              AND t.quality_r_multiple IS NOT NULL
              AND t.side IN ('BUY','SELL')
              AND t.session_bucket IN (
                    'asia','london','london_new_york_overlap','new_york','rollover',
                    'europe','ny_early','other'
                  )
              AND a.signal_posted_at<=:cutoff
              AND a.aidy_context_as_of_utc<=a.signal_posted_at
              AND a.provider_profile_effective_at<=a.signal_posted_at
              AND a.regime_json->>'regime_definition_version'=:regime_version
            ORDER BY t.source_id,a.signal_posted_at,t.id
            """
        ),
        {"cutoff": cutoff, "regime_version": day13.AIDY_REGIME_DEFINITION_VERSION},
    ).mappings().all()

    observations: list[day13.ConditionalObservation] = []
    for row in rows:
        duration_seconds = (row["closed_at"] - row["opened_at"]).total_seconds()
        if duration_seconds < 0:
            continue
        labels = day13._known_regime_labels(row["regime_json"])
        if not labels:
            continue
        observations.append(
            day13.ConditionalObservation(
                trade_id=UUID(str(row["trade_id"])),
                source_id=UUID(str(row["source_id"])),
                signal_id=UUID(str(row["signal_id"])),
                side=str(row["side"]),
                session_bucket=str(row["day13_session_bucket"]),
                signal_posted_at=row["signal_posted_at"].astimezone(UTC),
                duration_bucket=day13.duration_bucket(duration_seconds),
                realized_r=float(row["quality_r_multiple"]),
                regime_labels=labels,
            )
        )
    return observations


def run() -> dict[str, Any]:
    day13._load_observations = _load_observations
    return day13.run()


if __name__ == "__main__":
    run()
