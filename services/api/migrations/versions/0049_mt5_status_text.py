"""Remove PostgreSQL bind-type ambiguity from MT5 status persistence.

Revision ID: 0049_mt5_status_text
Revises: 0048_mt5_profiles
Create Date: 2026-08-31

The MT5 connection state update intentionally reuses the status bind parameter in
both a column assignment and a CASE comparison. Psycopg/PostgreSQL can infer those
contexts as varchar and text respectively, raising AmbiguousParameter and turning a
valid MT5 connection attempt into HTTP 500. PostgreSQL text and varchar have the same
string semantics for these constrained status values, so storing the status columns
as text removes the ambiguity without changing allowed values or account data.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0049_mt5_status_text"
down_revision: str | None = "0048_mt5_profiles"
branch_labels: str | Sequence[str] | None = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE mt5_accounts ALTER COLUMN status TYPE text USING status::text"
    )
    op.execute(
        "ALTER TABLE mt5_account_profiles ALTER COLUMN status TYPE text USING status::text"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE mt5_account_profiles ALTER COLUMN status TYPE varchar(24) USING status::varchar(24)"
    )
    op.execute(
        "ALTER TABLE mt5_accounts ALTER COLUMN status TYPE varchar(24) USING status::varchar(24)"
    )
