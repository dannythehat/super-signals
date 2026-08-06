"""Add the role and permission matrix.

Revision ID: 0003_role_permissions
Revises: 0002_owner_auth
Create Date: 2026-08-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_role_permissions"
down_revision: str | None = "0002_owner_auth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSIONS = (
    ("profile.view", "shared", "View the signed-in account and security status."),
    ("users.manage", "owner", "Invite, suspend and revoke platform users."),
    ("access_keys.manage", "owner", "Create and revoke personal access keys."),
    ("admins.manage", "owner", "Grant or remove administrator permissions."),
    ("mt5_accounts.approve", "owner", "Approve or alter connected MT5 accounts."),
    ("security.manage", "owner", "Change core platform security settings."),
    ("settings.manage", "owner", "Change protected system settings."),
    ("environments.manage", "owner", "Control testing and live environments."),
    ("platform.delete", "owner", "Delete the platform."),
    ("sources.manage", "trading", "Add, remove, pause and reactivate Telegram sources."),
    ("sources.change_status", "trading", "Move sources between Testing and Live."),
    ("source_messages.view", "trading", "View original Telegram source messages."),
    ("signals.review", "trading", "Review parsed signals and unclear messages."),
    ("trades.review", "trading", "Review trade execution and failures."),
    ("activity.view", "trading", "View user trading activity and system events."),
    ("emergency_stop.use", "trading", "Use the emergency trading stop."),
    ("account.connect", "user", "Connect one approved Vantage MT5 account."),
    ("risk.manage", "user", "Choose the allowed risk per position."),
    ("automation.toggle", "user", "Activate or stop automated trading."),
    ("signals.view", "user", "View approved signals without source identity."),
    ("positions.view", "user", "View positions and trade status."),
    ("performance.view", "user", "View trading performance."),
    ("passkey.manage", "user", "Set up device passkey security."),
)

ROLE_PERMISSIONS = {
    "owner": tuple(code for code, _, _ in PERMISSIONS),
    "trading_admin": (
        "profile.view",
        "sources.manage",
        "sources.change_status",
        "source_messages.view",
        "signals.review",
        "trades.review",
        "activity.view",
        "emergency_stop.use",
    ),
    "user": (
        "profile.view",
        "account.connect",
        "risk.manage",
        "automation.toggle",
        "signals.view",
        "positions.view",
        "performance.view",
        "passkey.manage",
    ),
}


def upgrade() -> None:
    op.create_table(
        "permissions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("code", sa.String(length=80), nullable=False),
        sa.Column("area", sa.String(length=20), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "area IN ('shared', 'owner', 'trading', 'user')",
            name="ck_permissions_area",
        ),
        sa.UniqueConstraint("code", name="uq_permissions_code"),
    )
    op.create_table(
        "role_permissions",
        sa.Column(
            "role_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("roles.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "permission_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("permissions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    bind = op.get_bind()
    role_descriptions = {
        "owner": "Full platform control, security, users, keys and roles.",
        "trading_admin": "Signal sources, tests, activity and emergency trading controls.",
        "user": "Personal MT5 connection, risk, automation, signals and performance.",
    }
    for role_name, description in role_descriptions.items():
        bind.execute(
            sa.text(
                """
                INSERT INTO roles (name, description)
                VALUES (:name, :description)
                ON CONFLICT (name)
                DO UPDATE SET description = EXCLUDED.description
                """
            ),
            {"name": role_name, "description": description},
        )

    permissions_table = sa.table(
        "permissions",
        sa.column("code", sa.String()),
        sa.column("area", sa.String()),
        sa.column("description", sa.Text()),
    )
    op.bulk_insert(
        permissions_table,
        [
            {"code": code, "area": area, "description": description}
            for code, area, description in PERMISSIONS
        ],
    )

    for role_name, permission_codes in ROLE_PERMISSIONS.items():
        for permission_code in permission_codes:
            bind.execute(
                sa.text(
                    """
                    INSERT INTO role_permissions (role_id, permission_id)
                    SELECT r.id, p.id
                    FROM roles AS r
                    JOIN permissions AS p ON p.code = :permission_code
                    WHERE r.name = :role_name
                    ON CONFLICT DO NOTHING
                    """
                ),
                {
                    "role_name": role_name,
                    "permission_code": permission_code,
                },
            )

    op.execute("ALTER TABLE permissions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE role_permissions ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_table("role_permissions")
    op.drop_table("permissions")
