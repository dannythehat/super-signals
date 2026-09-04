"""Allow member access to be paused without deleting account connections.

Revision ID: 0054_pause_member_access
Revises: 0053_revoke_removed_russian
Create Date: 2026-09-04

A pause is intentionally different from revocation: it blocks new subscription-gated
activity while preserving the member, MT5 connection, settings and history for a later
resume.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0054_pause_member_access"
down_revision: str | None = "0053_revoke_removed_russian"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_complimentary_access_status",
        "complimentary_access_grants",
        type_="check",
    )
    op.create_check_constraint(
        "ck_complimentary_access_status",
        "complimentary_access_grants",
        "status IN ('active','suspended','revoked')",
    )


def downgrade() -> None:
    # A downgrade cannot represent a paused complimentary grant. Preserve the safe,
    # inactive meaning rather than silently reactivating it.
    op.execute(
        """
        UPDATE complimentary_access_grants
        SET status='revoked', revoked_at=COALESCE(revoked_at, now()), updated_at=now()
        WHERE status='suspended'
        """
    )
    op.drop_constraint(
        "ck_complimentary_access_status",
        "complimentary_access_grants",
        type_="check",
    )
    op.create_check_constraint(
        "ck_complimentary_access_status",
        "complimentary_access_grants",
        "status IN ('active','revoked')",
    )
