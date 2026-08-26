"""Add audited reporting override for the 26 August 2026 incident.

Revision ID: 0029_reporting_overrides
Revises: 0028_retire_tdc_provider
Create Date: 2026-08-26

Raw broker deals, positions, signals and canonical outcomes remain untouched. The
override is an explicit reporting-layer correction for the connected demo account.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0029_reporting_overrides"
down_revision: str | None = "0028_retire_tdc_provider"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INCIDENT_KEY = "incident-2026-08-26-trade-management"


def upgrade() -> None:
    op.create_table(
        "performance_reporting_overrides",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("reporting_date", sa.Date(), nullable=False),
        sa.Column(
            "timezone",
            sa.String(length=64),
            nullable=False,
            server_default="Europe/Sofia",
        ),
        sa.Column("realised_cash_pnl", sa.Numeric(14, 2), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("incident_key", sa.String(length=96), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "user_id",
            "reporting_date",
            name="uq_performance_reporting_override_user_day",
        ),
        sa.UniqueConstraint(
            "incident_key",
            name="uq_performance_reporting_override_incident",
        ),
    )
    op.create_index(
        "ix_performance_reporting_overrides_user_date",
        "performance_reporting_overrides",
        ["user_id", "reporting_date"],
    )

    reason = (
        "26 August 2026 incident correction: exclude application-affected daily "
        "results and use the reviewed pre-incident result of +$35.00."
    )
    op.execute(
        sa.text(
            """
            INSERT INTO performance_reporting_overrides (
                user_id,
                reporting_date,
                timezone,
                realised_cash_pnl,
                reason,
                incident_key
            )
            SELECT owner_user_id, DATE '2026-08-26', 'Europe/Sofia', 35.00, :reason, :key
            FROM mt5_accounts
            WHERE metaapi_account_id='9bcef441-1821-4479-88c7-bcf3d210e9a3'
            ON CONFLICT (incident_key) DO NOTHING
            """
        ).bindparams(reason=reason, key=_INCIDENT_KEY)
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
        ).bindparams(key=_INCIDENT_KEY)
    )


def downgrade() -> None:
    op.drop_index(
        "ix_performance_reporting_overrides_user_date",
        table_name="performance_reporting_overrides",
    )
    op.drop_table("performance_reporting_overrides")
