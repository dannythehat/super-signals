"""Remove unresolved performance rows which have no broker evidence.

Revision ID: 0042_no_broker_evidence
Revises: 0041_unfilled_outcomes
Create Date: 2026-08-31

A canonical broker-backed performance outcome must have either a mapped broker
position or stored broker deal evidence. Planned rows which never received a broker
order/position are not trades and must never become ``closed_unknown``.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0042_no_broker_evidence"
down_revision: str | None = "0041_unfilled_outcomes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM performance_trade_outcomes AS o
        USING positions AS p
        WHERE o.position_id = p.id
          AND o.status = 'closed_unknown'
          AND p.broker_position_id IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM broker_deals AS d WHERE d.position_id = p.id
          )
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_unfilled_performance_outcome()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            mapped_position_id text;
            local_status text;
            has_broker_deal boolean;
        BEGIN
            IF NEW.status <> 'closed_unknown' THEN
                RETURN NEW;
            END IF;

            SELECT
                p.broker_position_id,
                p.status,
                EXISTS(SELECT 1 FROM broker_deals AS d WHERE d.position_id = p.id)
            INTO mapped_position_id, local_status, has_broker_deal
            FROM positions AS p
            WHERE p.id = NEW.position_id;

            IF local_status IN ('error','skipped','cancelled') THEN
                RETURN NULL;
            END IF;

            IF mapped_position_id IS NULL AND NOT COALESCE(has_broker_deal,false) THEN
                RETURN NULL;
            END IF;

            RETURN NEW;
        END;
        $$
        """
    )


def downgrade() -> None:
    # Keep the stricter guard in place on downgrade; reintroducing fake settling
    # rows would corrupt member-facing performance.
    pass
