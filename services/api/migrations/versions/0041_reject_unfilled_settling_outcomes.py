"""Keep cancelled/unfilled broker orders out of trading performance.

Revision ID: 0041_reject_unfilled_settling_outcomes
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
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0041_reject_unfilled_settling_outcomes"
down_revision: str | None = "0040_retire_rikke_admin_login"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The execution layer already uses `cancelled` when a broker pending order is
    # successfully cancelled. Make that terminal state legal instead of allowing
    # a successful broker mutation to leave a stale local `pending` row.
    op.execute("ALTER TABLE positions DROP CONSTRAINT IF EXISTS ck_positions_status")
    op.execute(
        """
        ALTER TABLE positions
        ADD CONSTRAINT ck_positions_status
        CHECK (status IN ('planned','pending','open','closed','cancelled','skipped','error'))
        """
    )

    # Purge only derived outcomes for orders which demonstrably never became a
    # broker position. Raw positions, broker order IDs and audit history remain.
    op.execute(
        """
        DELETE FROM performance_trade_outcomes AS o
        USING positions AS p
        WHERE o.position_id = p.id
          AND o.status = 'closed_unknown'
          AND p.broker_position_id IS NULL
          AND p.broker_order_id IS NOT NULL
          AND p.status IN ('closed','cancelled','skipped','error')
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

            IF mapped_position_id IS NULL
               AND local_order_id IS NOT NULL
               AND local_status IN ('closed','cancelled','skipped','error') THEN
                -- No broker position ever existed: this was a cancelled/unfilled
                -- order, not a trade and not a settlement diagnostic.
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
    # Do not narrow the position constraint on downgrade: rows may legitimately
    # already be `cancelled`, and making them invalid would break rollback.
