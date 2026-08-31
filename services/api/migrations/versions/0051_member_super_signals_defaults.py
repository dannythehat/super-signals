"""Auto-enable approved members with Super Signals trading defaults.

Revision ID: 0051_member_super_defaults
Revises: 0050_closed_unknown_safe
Create Date: 2026-08-31

Approved members should participate in Smart Signals automatically. They must not be
silently excluded because a separate user_trading_controls row was never created.
This migration backfills missing controls for currently entitled members and installs
triggers so future complimentary or paid activations receive the same defaults.

Existing controls are never overwritten. If a member has explicitly stopped trading or
changed their own controls, this migration preserves that choice.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0051_member_super_defaults"
down_revision: str | None = "0050_closed_unknown_safe"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO user_trading_controls (
            user_id,
            risk_percent,
            allow_double_lot,
            trading_status,
            activated_at,
            stopped_at,
            updated_at
        )
        SELECT DISTINCT
            u.id,
            1.0::numeric,
            TRUE,
            'active'::varchar,
            now(),
            NULL::timestamptz,
            now()
        FROM users AS u
        JOIN user_roles AS ur ON ur.user_id = u.id
        JOIN roles AS r ON r.id = ur.role_id AND r.name = 'user'
        LEFT JOIN member_subscriptions AS ms
          ON ms.user_id = u.id
         AND ms.status = 'active'
         AND ms.active_until > now()
        LEFT JOIN complimentary_access_grants AS cag
          ON cag.user_id = u.id
         AND cag.status = 'active'
        WHERE u.status = 'active'
          AND (ms.user_id IS NOT NULL OR cag.user_id IS NOT NULL)
        ON CONFLICT (user_id) DO NOTHING
        """
    )

    op.execute(
        """
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
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )

    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_complimentary_member_super_defaults
        ON complimentary_access_grants
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
        DROP TRIGGER IF EXISTS trg_subscription_member_super_defaults
        ON member_subscriptions
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


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_subscription_member_super_defaults
        ON member_subscriptions
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_complimentary_member_super_defaults
        ON complimentary_access_grants
        """
    )
    op.execute("DROP FUNCTION IF EXISTS ensure_member_super_signals_defaults()")
