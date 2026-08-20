"""Add audit-only suppressed notification audience.

Revision ID: 0026_notification_suppressed
Revises: 0025_pending_layered_entries
Create Date: 2026-08-20

Historical lifecycle notification rows are retained as forensic evidence, but stale
replays must be structurally excluded from member-facing channels. ``suppressed`` is an
explicit non-member audience state used by the canonical publisher cleanup.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0026_notification_suppressed"
down_revision: str | None = "0025_pending_layered_entries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_notification_events_audience", "notification_events", type_="check")
    op.create_check_constraint(
        "ck_notification_events_audience",
        "notification_events",
        "audience IN ('shared','user','suppressed')",
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM notification_events
                WHERE audience='suppressed'
            ) THEN
                RAISE EXCEPTION 'cannot downgrade: suppressed notification audit rows exist';
            END IF;
        END $$;
        """
    )
    op.drop_constraint("ck_notification_events_audience", "notification_events", type_="check")
    op.create_check_constraint(
        "ck_notification_events_audience",
        "notification_events",
        "audience IN ('shared','user')",
    )
