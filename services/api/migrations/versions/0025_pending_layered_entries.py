"""Add broker-pending and layered-entry position identity.

Revision ID: 0025_pending_layered_entries
Revises: 0024_day40_no_global_stop
Create Date: 2026-08-17

A signal can now map more than one explicit entry layer while retaining the existing
one-position-per-TP execution model. Risk is still configured per TP plan; each layer
stores its actual share of that TP risk so adding layers cannot silently multiply the
user's configured risk budget.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_pending_layered_entries"
down_revision: str | None = "0024_day40_no_global_stop"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "positions",
        sa.Column("entry_index", sa.SmallInteger(), nullable=False, server_default="1"),
    )
    op.add_column(
        "positions",
        sa.Column(
            "entry_order_type",
            sa.String(length=32),
            nullable=False,
            server_default="market",
        ),
    )
    # Preserve the provider-declared layer separately from the broker's actual fill.
    # This is essential for follow-ups such as "4394 CLOSE +15": market fills can
    # slip/fractionally fill while the provider continues to refer to the declared
    # layer price in later Telegram management messages.
    op.add_column(
        "positions",
        sa.Column("provider_entry_price", sa.Numeric(24, 10), nullable=True),
    )
    op.execute(
        "UPDATE positions SET provider_entry_price=entry_price "
        "WHERE provider_entry_price IS NULL AND entry_price IS NOT NULL"
    )

    op.drop_constraint("uq_positions_signal_user_tp", "positions", type_="unique")
    op.create_unique_constraint(
        "uq_positions_signal_user_entry_tp",
        "positions",
        ["signal_id", "user_id", "entry_index", "tp_index"],
    )
    op.create_check_constraint("ck_positions_entry_index", "positions", "entry_index > 0")
    op.create_check_constraint(
        "ck_positions_entry_order_type",
        "positions",
        "entry_order_type IN ('market','buy_limit','sell_limit','buy_stop','sell_stop')",
    )

    op.drop_constraint("ck_positions_status", "positions", type_="check")
    op.create_check_constraint(
        "ck_positions_status",
        "positions",
        "status IN ('planned','pending','open','closed','skipped','error')",
    )

    # Layer risk shares can be fractional (for example a configured 1.5% TP budget
    # split across two entries becomes 0.75% per layer). The user-facing risk choices
    # remain constrained elsewhere; this column records the actual tranche exposure.
    op.drop_constraint("ck_positions_risk_percent", "positions", type_="check")
    op.create_check_constraint(
        "ck_positions_risk_percent",
        "positions",
        "planned_risk_percent > 0 AND planned_risk_percent <= 4.0",
    )


def downgrade() -> None:
    # Downgrade is intentionally blocked if layered rows exist because collapsing
    # (entry_index,tp_index) back to tp_index alone would lose broker identity.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM positions WHERE entry_index <> 1) THEN
                RAISE EXCEPTION 'cannot downgrade: layered positions exist';
            END IF;
            IF EXISTS (SELECT 1 FROM positions WHERE status = 'pending') THEN
                RAISE EXCEPTION 'cannot downgrade: pending positions exist';
            END IF;
        END $$;
        """
    )

    op.drop_constraint("ck_positions_risk_percent", "positions", type_="check")
    op.create_check_constraint(
        "ck_positions_risk_percent",
        "positions",
        "planned_risk_percent IN (0.5,1.0,1.5,2.0,3.0,4.0)",
    )
    op.drop_constraint("ck_positions_status", "positions", type_="check")
    op.create_check_constraint(
        "ck_positions_status",
        "positions",
        "status IN ('planned','open','closed','skipped','error')",
    )
    op.drop_constraint("ck_positions_entry_order_type", "positions", type_="check")
    op.drop_constraint("ck_positions_entry_index", "positions", type_="check")
    op.drop_constraint("uq_positions_signal_user_entry_tp", "positions", type_="unique")
    op.create_unique_constraint(
        "uq_positions_signal_user_tp",
        "positions",
        ["signal_id", "user_id", "tp_index"],
    )
    op.drop_column("positions", "provider_entry_price")
    op.drop_column("positions", "entry_order_type")
    op.drop_column("positions", "entry_index")
