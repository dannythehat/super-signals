"""Remove the obsolete global emergency-stop permission.

Revision ID: 0024_day40_remove_global_emergency_stop
Revises: 0023_day39_security_hardening
Create Date: 2026-08-13
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0024_day40_remove_global_emergency_stop"
down_revision: str | None = "0023_day39_security_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM role_permissions
        WHERE permission_id = (
            SELECT id FROM permissions WHERE code = 'emergency_stop.use'
        )
        """
    )
    op.execute("DELETE FROM permissions WHERE code = 'emergency_stop.use'")


def downgrade() -> None:
    op.execute(
        """
        INSERT INTO permissions (code, area, description)
        VALUES ('emergency_stop.use', 'trading', 'Use the emergency trading stop.')
        ON CONFLICT (code) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO role_permissions (role_id, permission_id)
        SELECT r.id, p.id
        FROM roles AS r
        CROSS JOIN permissions AS p
        WHERE r.name IN ('owner', 'trading_admin')
          AND p.code = 'emergency_stop.use'
        ON CONFLICT DO NOTHING
        """
    )
