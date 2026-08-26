"""Audited reporting overrides for incident-corrupted performance days.

Broker deals and canonical outcomes stay immutable. A reporting override removes the
affected local day's outcomes from user-facing aggregates and substitutes one reviewed
cash result. This keeps forensic truth while preventing known application failures from
distorting official performance.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session


OUTCOME_NOT_OVERRIDDEN_SQL = """
NOT EXISTS (
    SELECT 1
    FROM performance_reporting_overrides AS reporting_override
    WHERE reporting_override.user_id=o.user_id
      AND o.closed_at IS NOT NULL
      AND (o.closed_at AT TIME ZONE reporting_override.timezone)::date
          = reporting_override.reporting_date
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


def current_day_override(
    session: Session,
    user_id: UUID,
    *,
    day_start: datetime,
) -> tuple[Decimal, str] | None:
    """Return the override matching the supplied local-day UTC boundary."""
    row = session.execute(
        text(
            """
            SELECT realised_cash_pnl, reason
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
    return Decimal(str(row["realised_cash_pnl"])), str(row["reason"])


__all__ = [
    "OUTCOME_NOT_OVERRIDDEN_SQL",
    "current_day_override",
    "override_cash_for_window",
]
