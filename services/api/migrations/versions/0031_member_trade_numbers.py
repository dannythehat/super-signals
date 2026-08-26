"""Add reusable member-facing trade numbers.

Revision ID: 0031_member_trade_numbers
Revises: 0030_shadow_sources
"""

import sqlalchemy as sa
from alembic import op

revision = "0031_member_trade_numbers"
down_revision = "0030_shadow_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("signals", sa.Column("member_trade_number", sa.SmallInteger(), nullable=True))
    op.create_check_constraint(
        "ck_signals_member_trade_number_positive",
        "signals",
        "member_trade_number IS NULL OR member_trade_number > 0",
    )
    op.create_index(
        "ix_signals_member_trade_number",
        "signals",
        ["member_trade_number"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_signals_member_trade_number", table_name="signals")
    op.drop_constraint(
        "ck_signals_member_trade_number_positive",
        "signals",
        type_="check",
    )
    op.drop_column("signals", "member_trade_number")
