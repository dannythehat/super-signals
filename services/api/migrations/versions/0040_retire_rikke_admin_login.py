"""Retire legacy Rikke admin login while preserving Telegram/source ownership.

Revision ID: 0040_retire_rikke_admin_login
Revises: 0039_complimentary_member_access
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0040_retire_rikke_admin_login"
down_revision: str | None = "0039_complimentary_member_access"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TARGET_EMAIL = "rikkevenoebo@hotmail.com"


def upgrade() -> None:
    connection = op.get_bind()
    user_id = connection.execute(
        sa.text(
            """
            SELECT id
            FROM users
            WHERE lower(email::text) = lower(:email)
            LIMIT 1
            """
        ),
        {"email": _TARGET_EMAIL},
    ).scalar_one_or_none()

    if user_id is None:
        return

    retired_email = f"retired-rikke-{str(user_id).replace('-', '')}@internal.invalid"

    # Preserve the user row so Telegram accounts, source ownership, reader access,
    # audit history and any other foreign-key relationships remain intact.
    # Only the old administrative login identity is retired.
    connection.execute(
        sa.text("DELETE FROM auth_sessions WHERE user_id = :user_id"),
        {"user_id": user_id},
    )
    connection.execute(
        sa.text("DELETE FROM user_roles WHERE user_id = :user_id"),
        {"user_id": user_id},
    )
    connection.execute(
        sa.text(
            """
            UPDATE users
            SET email = :retired_email,
                status = 'revoked',
                password_hash = NULL,
                two_factor_enabled = false,
                passkey_enabled = false,
                updated_at = now()
            WHERE id = :user_id
            """
        ),
        {"user_id": user_id, "retired_email": retired_email},
    )


def downgrade() -> None:
    # Intentional irreversible production identity cleanup. Restoring the legacy
    # administrator login could re-enable credentials and permissions that were
    # explicitly retired, so downgrade leaves the cleaned identity unchanged.
    pass
