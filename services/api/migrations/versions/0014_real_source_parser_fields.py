"""Persist real-source parser entry ranges and order type.

Revision ID: 0014_real_source_parser_fields
Revises: 0013_day26_position_execution
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_real_source_parser_fields"
down_revision: str | None = "0013_day26_position_execution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("message_parses", sa.Column("entry_low", sa.Numeric(24, 10), nullable=True))
    op.add_column("message_parses", sa.Column("entry_high", sa.Numeric(24, 10), nullable=True))
    op.add_column(
        "message_parses",
        sa.Column("order_type", sa.String(12), nullable=True),
    )
    op.create_check_constraint(
        "ck_message_parses_order_type",
        "message_parses",
        "order_type IS NULL OR order_type IN ('market', 'pending')",
    )
    op.create_check_constraint(
        "ck_message_parses_entry_range",
        "message_parses",
        "entry_high IS NULL OR entry_low IS NULL OR entry_high >= entry_low",
    )


def downgrade() -> None:
    op.drop_constraint("ck_message_parses_entry_range", "message_parses", type_="check")
    op.drop_constraint("ck_message_parses_order_type", "message_parses", type_="check")
    op.drop_column("message_parses", "order_type")
    op.drop_column("message_parses", "entry_high")
    op.drop_column("message_parses", "entry_low")
