"""Make unresolved broker outcomes permanently non-actionable.

Revision ID: 0050_closed_unknown_safe
Revises: 0049_mt5_status_text
Create Date: 2026-08-31

`closed_unknown` is diagnostic uncertainty, not proof that a broker position closed.
It must never close a local live position or contribute a final result. Only a
broker-confirmed exit outcome (won/lost/breakeven) may settle a mapped trade.

This migration also repairs rows that the legacy Day 34 settlement watcher may have
falsely marked closed from `closed_unknown` without any broker exit deal. The repair
changes local application state only; it never places, closes, or modifies a broker
trade.

Signal lifecycle events are intentionally append-only, so historical bad events are
not deleted here. The repaired position/outcome state is authoritative and future
unknown settlement events are prevented by the guards below.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0050_closed_unknown_safe"
down_revision: str | None = "0049_mt5_status_text"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Repair only positions that the broker-settlement watcher itself falsely closed.
    # Broker state is untouched. These rows become locally open again until an actual
    # exit deal is observed.
    op.execute(
        """
        UPDATE positions AS p
        SET status = 'open',
            closed_at = NULL,
            exit_price = NULL,
            pnl_amount = NULL,
            pnl_percent = NULL,
            close_reason = NULL,
            updated_at = now()
        FROM performance_trade_outcomes AS o
        WHERE o.position_id = p.id
          AND o.status = 'closed_unknown'
          AND p.status = 'closed'
          AND p.close_reason = 'broker_settled'
          AND p.broker_position_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM broker_deals AS d
              WHERE d.position_id = p.id
                AND UPPER(COALESCE(d.entry_type,'')) IN ('DEAL_ENTRY_OUT','DEAL_ENTRY_OUT_BY')
          )
        """
    )

    # Unknown outcomes are not outcomes. Remove unresolved rows so the ledger keeps
    # polling until real broker exit evidence arrives.
    op.execute(
        """
        DELETE FROM performance_trade_outcomes AS o
        USING positions AS p
        WHERE o.position_id = p.id
          AND o.status = 'closed_unknown'
          AND NOT EXISTS (
              SELECT 1
              FROM broker_deals AS d
              WHERE d.position_id = p.id
                AND UPPER(COALESCE(d.entry_type,'')) IN ('DEAL_ENTRY_OUT','DEAL_ENTRY_OUT_BY')
          )
        """
    )

    # Make the invariant permanent at the data boundary. A future code regression
    # cannot persist `closed_unknown` as a terminal performance row.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_unfilled_performance_outcome()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.status = 'closed_unknown' THEN
                RETURN NULL;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )

    # Even if invalid application code tries to settle a live position, block the
    # transition unless a decided broker outcome backed by broker deals exists.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_unconfirmed_broker_settlement_close()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF OLD.status = 'open'
               AND NEW.status = 'closed'
               AND COALESCE(NEW.close_reason,'') = 'broker_settled'
               AND NOT EXISTS (
                   SELECT 1
                   FROM performance_trade_outcomes AS o
                   WHERE o.position_id = OLD.id
                     AND o.status IN ('won','lost','breakeven')
                     AND o.broker_deal_count > 0
               )
            THEN
                NEW.status := OLD.status;
                NEW.closed_at := OLD.closed_at;
                NEW.exit_price := OLD.exit_price;
                NEW.pnl_amount := OLD.pnl_amount;
                NEW.pnl_percent := OLD.pnl_percent;
                NEW.close_reason := OLD.close_reason;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_prevent_unconfirmed_broker_settlement_close
        ON positions
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_prevent_unconfirmed_broker_settlement_close
        BEFORE UPDATE ON positions
        FOR EACH ROW
        EXECUTE FUNCTION prevent_unconfirmed_broker_settlement_close()
        """
    )


def downgrade() -> None:
    # Safety invariant is intentionally retained on downgrade. Re-enabling an
    # uncertainty-driven close would make broker-held live trades unsafe.
    pass
