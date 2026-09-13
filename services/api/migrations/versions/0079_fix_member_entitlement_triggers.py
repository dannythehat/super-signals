"""Fix entitlement-trigger field access for complimentary members.

Revision ID: 0079_fix_member_entitlements
Revises: 0078_aidy_intel_bf
Create Date: 2026-09-13

The 0051 shared trigger function referenced NEW.active_until even when invoked from
complimentary_access_grants, which has no active_until column. PostgreSQL resolves
that record field and raises UndefinedColumn. Split the two triggers so each function
references only columns present on its own table.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0079_fix_member_entitlements"
down_revision: str | None = "0078_aidy_intel_bf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_DEFAULT_CONTROLS_SQL = """
    INSERT INTO user_trading_controls (
        user_id,
        risk_percent,
        allow_double_lot,
        trading_status,
        activated_at,
        stopped_at,
        updated_at
    ) VALUES (
        NEW.user_id,
        1.0,
        TRUE,
        'active',
        now(),
        NULL::timestamptz,
        now()
    )
    ON CONFLICT (user_id) DO NOTHING;
"""


def upgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_complimentary_member_super_defaults
        ON complimentary_access_grants
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_subscription_member_super_defaults
        ON member_subscriptions
        """
    )
    op.execute("DROP FUNCTION IF EXISTS ensure_member_super_signals_defaults()")

    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION ensure_complimentary_member_super_signals_defaults()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.status = 'active' THEN
                {_DEFAULT_CONTROLS_SQL}
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION ensure_subscription_member_super_signals_defaults()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.status = 'active'
               AND NEW.active_until IS NOT NULL
               AND NEW.active_until > now()
            THEN
                {_DEFAULT_CONTROLS_SQL}
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )

    op.execute(
        """
        CREATE TRIGGER trg_complimentary_member_super_defaults
        AFTER INSERT OR UPDATE OF status ON complimentary_access_grants
        FOR EACH ROW
        EXECUTE FUNCTION ensure_complimentary_member_super_signals_defaults()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_subscription_member_super_defaults
        AFTER INSERT OR UPDATE OF status, active_until ON member_subscriptions
        FOR EACH ROW
        EXECUTE FUNCTION ensure_subscription_member_super_signals_defaults()
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_complimentary_member_super_defaults
        ON complimentary_access_grants
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_subscription_member_super_defaults
        ON member_subscriptions
        """
    )
    op.execute("DROP FUNCTION IF EXISTS ensure_complimentary_member_super_signals_defaults()")
    op.execute("DROP FUNCTION IF EXISTS ensure_subscription_member_super_signals_defaults()")

    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION ensure_member_super_signals_defaults()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.status = 'active' THEN
                IF TG_TABLE_NAME = 'member_subscriptions'
                   AND NEW.active_until IS NOT NULL
                   AND NEW.active_until <= now()
                THEN
                    RETURN NEW;
                END IF;
                {_DEFAULT_CONTROLS_SQL}
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_complimentary_member_super_defaults
        AFTER INSERT OR UPDATE OF status ON complimentary_access_grants
        FOR EACH ROW
        EXECUTE FUNCTION ensure_member_super_signals_defaults()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_subscription_member_super_defaults
        AFTER INSERT OR UPDATE OF status, active_until ON member_subscriptions
        FOR EACH ROW
        EXECUTE FUNCTION ensure_member_super_signals_defaults()
        """
    )
