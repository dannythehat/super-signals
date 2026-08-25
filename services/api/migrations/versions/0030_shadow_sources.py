"""Add isolated shadow-provider evaluation.

Revision ID: 0030_shadow_sources
Revises: 0029_retire_stale_matthew
"""

from alembic import op

revision = "0030_shadow_sources"
down_revision = "0029_retire_stale_matthew"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_sources_status", "sources", type_="check")
    op.create_check_constraint(
        "ck_sources_status", "sources",
        "status IN ('testing', 'shadow', 'paused', 'live', 'revoked')",
    )
    op.execute("""
        CREATE TABLE shadow_trades (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            source_id uuid NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            signal_id uuid NOT NULL UNIQUE REFERENCES signals(id) ON DELETE CASCADE,
            message_id uuid NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            symbol varchar(40) NOT NULL,
            side varchar(4) NOT NULL CHECK (side IN ('BUY','SELL')),
            order_type varchar(12) NOT NULL CHECK (order_type IN ('market','pending')),
            entry_low numeric(24,10), entry_high numeric(24,10),
            initial_stop numeric(24,10) NOT NULL, current_stop numeric(24,10) NOT NULL,
            take_profits jsonb NOT NULL DEFAULT '[]'::jsonb,
            hit_targets jsonb NOT NULL DEFAULT '[]'::jsonb,
            status varchar(20) NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','open','closed','cancelled','missed')),
            entry_price numeric(24,10), last_price numeric(24,10),
            max_price numeric(24,10), min_price numeric(24,10),
            realized_percent numeric(14,6) NOT NULL DEFAULT 0,
            pnl_percent numeric(14,6), close_reason varchar(100),
            opened_at timestamptz, closed_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.create_index("ix_shadow_trades_source_status", "shadow_trades", ["source_id", "status"])


def downgrade() -> None:
    op.drop_table("shadow_trades")
    op.drop_constraint("ck_sources_status", "sources", type_="check")
    op.create_check_constraint(
        "ck_sources_status", "sources",
        "status IN ('testing', 'paused', 'live', 'revoked')",
    )
