"""Keep cancelled/unfilled broker orders out of trading performance.

Revision ID: 0041_unfilled_outcomes
Revises: 0040_retire_rikke_admin_login
Create Date: 2026-08-31

A pending broker order that never became a broker position is not a trade. Older
performance rebuilding classified any locally closed row without a decided broker
deal as ``closed_unknown``. That caused provider-cancelled and compensating-rollback
orders to appear as user-facing settling trades even though they never opened.

The automatic protection path also uses ``cancelled`` as the terminal local status
for a broker order which was successfully cancelled. The original positions status
constraint did not allow that value, so the broker cancellation could succeed while
the local row remained pending. This migration makes ``cancelled`` a first-class
terminal state and excludes it from performance.

Rows already marked local ``error`` are diagnostic evidence, not active trades. A
future complete broker deal can still rebuild such a row as won/lost/breakeven, but
an unresolved ``closed_unknown`` error must never appear to members as settling.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0041_unfilled_outcomes"
down_revision: str | None = "0040_retire_rikke_admin_login"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE positions DROP CONSTRAINT IF EXISTS ck_positions_status")
    op.execute(
        """
        ALTER TABLE positions
        ADD CONSTRAINT ck_positions_status
        CHECK (status IN ('planned','pending','open','closed','cancelled','skipped','error'))
        """
    )

    op.execute(
        """
        DELETE FROM performance_trade_outcomes AS o
        USING positions AS p
        WHERE o.position_id = p.id
          AND o.status = 'closed_unknown'
          AND (
                (
                    p.broker_position_id IS NULL
                    AND p.broker_order_id IS NOT NULL
                    AND p.status IN ('closed','cancelled','skipped','error')
                )
                OR p.status IN ('error','skipped','cancelled')
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
            local_order_id text;
        BEGIN
            IF NEW.status <> 'closed_unknown' THEN
                RETURN NEW;
            END IF;

            SELECT p.broker_position_id, p.status, p.broker_order_id
            INTO mapped_position_id, local_status, local_order_id
            FROM positions AS p
            WHERE p.id = NEW.position_id;

            IF local_status IN ('error','skipped','cancelled') THEN
                RETURN NULL;
            END IF;

            IF mapped_position_id IS NULL
               AND local_order_id IS NOT NULL
               AND local_status = 'closed' THEN
                RETURN NULL;
            END IF;

            RETURN NEW;
        END;
        $$
        """
    )

    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_reject_unfilled_performance_outcome
        ON performance_trade_outcomes
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_reject_unfilled_performance_outcome
        BEFORE INSERT OR UPDATE ON performance_trade_outcomes
        FOR EACH ROW
        EXECUTE FUNCTION reject_unfilled_performance_outcome()
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_reject_unfilled_performance_outcome
        ON performance_trade_outcomes
        """
    )
    op.execute("DROP FUNCTION IF EXISTS reject_unfilled_performance_outcome()")
