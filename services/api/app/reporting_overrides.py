"""Audited reporting overrides for incident-corrupted performance days.

Broker deals and canonical outcomes stay immutable. A reporting override removes the
affected local day's pre-cutoff outcomes from user-facing aggregates and substitutes one
reviewed cash result. This keeps forensic truth while preventing known application
failures from distorting official performance.

Overrides whose incident_key starts with ``restart-`` are stronger clean-start boundaries:
all trades opened before the cutoff are excluded from the restart day onward, even if an
old position closes after the cutoff. Older completed historical days remain intact.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session


OUTCOME_NOT_OVERRIDDEN_SQL = """
NOT EXISTS (
    SELECT 1
    FROM performance_reporting_overrides AS reporting_override
    WHERE reporting_override.user_id=o.user_id
      AND (
        (
          reporting_override.incident_key NOT LIKE 'restart-%'
          AND o.closed_at IS NOT NULL
          AND o.closed_at < reporting_override.cutoff_at
          AND (o.closed_at AT TIME ZONE reporting_override.timezone)::date
              = reporting_override.reporting_date
        )
        OR
        (
          reporting_override.incident_key LIKE 'restart-%'
          AND COALESCE(o.opened_at,o.closed_at,o.derived_at) < reporting_override.cutoff_at
          AND (
            o.status IN ('open','pending')
            OR COALESCE(o.closed_at,o.derived_at,o.opened_at)
               >= (reporting_override.reporting_date::timestamp
                   AT TIME ZONE reporting_override.timezone)
          )
        )
      )
)
"""

# Broker-deal equivalent of OUTCOME_NOT_OVERRIDDEN_SQL. User-facing cash accounting is
# broker-backed, so incident days must exclude the same pre-cutoff cash without deleting
# or rewriting the immutable broker deal itself. Clean restart overrides additionally
# exclude exits belonging to positions opened before the restart boundary.
BROKER_DEAL_NOT_OVERRIDDEN_SQL = """
NOT EXISTS (
    SELECT 1
    FROM performance_reporting_overrides AS reporting_override
    WHERE reporting_override.user_id=bd.user_id
      AND (
        (
          reporting_override.incident_key NOT LIKE 'restart-%'
          AND bd.occurred_at < reporting_override.cutoff_at
          AND (bd.occurred_at AT TIME ZONE reporting_override.timezone)::date
              = reporting_override.reporting_date
        )
        OR
        (
          reporting_override.incident_key LIKE 'restart-%'
          AND bd.occurred_at >= (
                reporting_override.reporting_date::timestamp
                AT TIME ZONE reporting_override.timezone
              )
          AND EXISTS (
              SELECT 1
              FROM positions AS restart_position
              WHERE restart_position.user_id=bd.user_id
                AND restart_position.id=bd.position_id
                AND COALESCE(restart_position.opened_at,restart_position.created_at)
                    < reporting_override.cutoff_at
          )
        )
      )
)
"""


def override_cash_for_window(
    session: Session,
    user_id: UUID,
    *,
    start: datetime,
    end: datetime,
) -> Decimal:
    """Return reviewed daily cash amounts whose local midnight is in the window."""
    value = session.execute(
        text(
            """
            SELECT COALESCE(SUM(realised_cash_pnl), 0)
            FROM performance_reporting_overrides
            WHERE user_id=:user_id
              AND (reporting_date::timestamp AT TIME ZONE timezone)>=:start
              AND (reporting_date::timestamp AT TIME ZONE timezone)<:end
            """
        ),
        {"user_id": user_id, "start": start, "end": end},
    ).scalar_one()
    return Decimal(str(value or 0))


def override_cash_by_day(
    session: Session,
    user_id: UUID,
    *,
    start: datetime,
    end: datetime,
) -> dict[date, Decimal]:
    """Return reviewed cash keyed by the override's official local reporting date."""
    rows = session.execute(
        text(
            """
            SELECT reporting_date, realised_cash_pnl
            FROM performance_reporting_overrides
            WHERE user_id=:user_id
              AND (reporting_date::timestamp AT TIME ZONE timezone)>=:start
              AND (reporting_date::timestamp AT TIME ZONE timezone)<:end
            ORDER BY reporting_date
            """
        ),
        {"user_id": user_id, "start": start, "end": end},
    ).mappings().all()
    return {
        row["reporting_date"]: Decimal(str(row["realised_cash_pnl"] or 0))
        for row in rows
    }


def current_day_override(
    session: Session,
    user_id: UUID,
    *,
    day_start: datetime,
) -> tuple[Decimal, str, datetime] | None:
    """Return the override matching the supplied local-day UTC boundary."""
    row = session.execute(
        text(
            """
            SELECT realised_cash_pnl, reason, cutoff_at
            FROM performance_reporting_overrides
            WHERE user_id=:user_id
              AND reporting_date=(:day_start AT TIME ZONE timezone)::date
            LIMIT 1
            """
        ),
        {"user_id": user_id, "day_start": day_start},
    ).mappings().first()
    if row is None:
        return None
    cutoff_at = row["cutoff_at"]
    return Decimal(str(row["realised_cash_pnl"])), str(row["reason"]), cutoff_at


__all__ = [
    "BROKER_DEAL_NOT_OVERRIDDEN_SQL",
    "OUTCOME_NOT_OVERRIDDEN_SQL",
    "current_day_override",
    "override_cash_by_day",
    "override_cash_for_window",
]
