"""Audited reporting override for the 1 October 2026 demo-account compromise.

Owner decision, 2026-10-01. Between 07:11 and 07:23 UTC the owner's Vantage DEMO account
(MetaApi id below, the same connected demo as the 26 August override) was traded from
outside Super Signals: its balance fell from USD 2,309.66 to USD 21.74. Super Signals' own
positions in that window were 0.01 lot at 1% risk and are not the cause. The owner had
Vantage restore the demo to USD 2,300.00 (first seen by Super Signals at 07:38 UTC).

Reviewed result for the Europe/Sofia reporting day 2026-10-01: +384.84, which is the
account's verified day change (21:00 Sofia close on 30 September USD 1,915.16 -> USD
2,300.00). It is deliberately labelled in the reason: it includes account movement that
was not made by Super Signals trades. Super Signals' own recorded exits before the cutoff
net -97.51 for the day; those deals stay in the immutable broker evidence and are only
excluded from user-facing aggregates for that day, exactly as for the earlier overrides.
Exits after the cutoff count normally on top.

This is a reporting-layer correction only. It does not touch broker deals, positions,
signals, snapshots, balances or risk sizing (the balance is the broker's, unmodified).
It only inserts rows and never raises, so it can never take startup down.

Revision ID: 0125_incident_20261001_override
Revises: 0124_post_exec_edit
Create Date: 2026-10-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0125_incident_20261001_override"
down_revision: str | None = "0124_post_exec_edit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INCIDENT_KEY = "incident-2026-10-01-demo-compromise"
OWNER_DEMO_METAAPI_ACCOUNT_ID = "9bcef441-1821-4479-88c7-bcf3d210e9a3"
REPORTING_DATE = "2026-10-01"
REVIEWED_CASH_PNL = "384.84"
CUTOFF_AT = "2026-10-01T07:45:00+00:00"
REASON = (
    "1 October 2026 incident correction: unauthorised trading on the owner's Vantage demo "
    "account between 07:11 and 07:23 UTC cut the balance from $2,309.66 to $21.74; Vantage "
    "restored it to $2,300.00 at about 07:38 UTC. Reviewed day result +$384.84 is the "
    "account's verified change from the 30 September 21:00 Sofia close ($1,915.16) to "
    "$2,300.00. It includes account movement that was not made by Super Signals trades "
    "(Super Signals' own recorded exits before the cutoff netted -$97.51). Raw broker "
    "evidence is preserved; exits after 07:45 UTC count normally."
)


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            INSERT INTO performance_reporting_overrides (
                user_id,
                reporting_date,
                timezone,
                realised_cash_pnl,
                reason,
                incident_key,
                cutoff_at
            )
            SELECT owner_user_id,
                   CAST(:reporting_date AS date),
                   'Europe/Sofia',
                   CAST(:cash AS numeric),
                   :reason,
                   :key,
                   CAST(:cutoff AS timestamptz)
            FROM mt5_accounts
            WHERE metaapi_account_id=:metaapi_id
            ON CONFLICT DO NOTHING
            """
        ).bindparams(
            reporting_date=REPORTING_DATE,
            cash=REVIEWED_CASH_PNL,
            reason=REASON,
            key=INCIDENT_KEY,
            cutoff=CUTOFF_AT,
            metaapi_id=OWNER_DEMO_METAAPI_ACCOUNT_ID,
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO audit_events (
                actor_user_id,
                event_type,
                entity_type,
                entity_id,
                payload
            )
            SELECT
                user_id,
                'performance.reporting_override_created',
                'performance_reporting_override',
                id,
                jsonb_build_object(
                    'incident_key', incident_key,
                    'reporting_date', reporting_date,
                    'timezone', timezone,
                    'realised_cash_pnl', realised_cash_pnl,
                    'cutoff_at', cutoff_at,
                    'reason', reason,
                    'raw_broker_evidence_preserved', true
                )
            FROM performance_reporting_overrides
            WHERE incident_key=:key
              AND NOT EXISTS (
                  SELECT 1
                  FROM audit_events
                  WHERE event_type='performance.reporting_override_created'
                    AND entity_id=performance_reporting_overrides.id
              )
            """
        ).bindparams(key=INCIDENT_KEY)
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM performance_reporting_overrides WHERE incident_key=:key"
        ).bindparams(key=INCIDENT_KEY)
    )
