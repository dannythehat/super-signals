"""Enforce forward-only provider-profile provenance for Provider Lab research.

Revision ID: 0057_provider_pit_boundary
Revises: 0056_provider_profile_versions
Create Date: 2026-09-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0057_provider_pit_boundary"
down_revision: str | None = "0056_provider_profile_versions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "shadow_trades",
        sa.Column(
            "provider_profile_version_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "shadow_trades",
        sa.Column("provider_profile_version_no", sa.Integer(), nullable=True),
    )
    op.add_column(
        "shadow_trades",
        sa.Column("provider_profile_effective_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "shadow_trades",
        sa.Column(
            "provider_profile_pit_status",
            sa.String(length=32),
            nullable=False,
            server_default="legacy_unresolvable",
        ),
    )
    op.create_foreign_key(
        "fk_shadow_trade_provider_profile_version",
        "shadow_trades",
        "provider_research_profile_versions",
        ["provider_profile_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_shadow_trades_provider_profile_version",
        "shadow_trades",
        ["provider_profile_version_id"],
    )

    # Resolve each existing research trade only against profile knowledge that
    # was already effective when that signal was posted.  No current-profile
    # fallback is allowed and no Day 7 bootstrap row is backdated.
    op.execute(
        """
        WITH matched AS (
            SELECT
                t.id AS trade_id,
                v.id AS version_id,
                v.version_no,
                v.effective_at,
                v.profile_snapshot
            FROM shadow_trades t
            LEFT JOIN LATERAL (
                SELECT id,version_no,effective_at,profile_snapshot
                FROM provider_research_profile_versions pv
                WHERE pv.source_id=t.source_id
                  AND pv.effective_at <= t.signal_posted_at
                ORDER BY pv.effective_at DESC,pv.version_no DESC
                LIMIT 1
            ) v ON true
        )
        UPDATE shadow_trades t
        SET provider_profile_version_id=m.version_id,
            provider_profile_version_no=m.version_no,
            provider_profile_effective_at=m.effective_at,
            provider_profile_pit_status=CASE
                WHEN m.version_id IS NULL THEN 'legacy_unresolvable'
                ELSE 'resolved'
            END,
            provider_style=CASE
                WHEN m.version_id IS NULL THEN 'unknown'
                ELSE COALESCE(m.profile_snapshot->>'style','unknown')
            END,
            interpretation_readiness_at_entry=CASE
                WHEN m.version_id IS NULL THEN 0
                ELSE COALESCE(
                    NULLIF(m.profile_snapshot->>'interpretation_readiness','')::numeric,
                    0
                )
            END,
            score_exclusion_reason=CASE
                WHEN m.version_id IS NULL AND t.score_eligible
                    THEN 'legacy_profile_unresolvable'
                ELSE t.score_exclusion_reason
            END,
            pnl_percent=CASE
                WHEN m.version_id IS NULL AND t.score_eligible THEN NULL
                ELSE t.pnl_percent
            END,
            score_eligible=CASE
                WHEN m.version_id IS NULL THEN false
                ELSE t.score_eligible
            END,
            updated_at=now()
        FROM matched m
        WHERE t.id=m.trade_id
        """
    )

    op.create_check_constraint(
        "ck_shadow_trade_provider_profile_pit_status",
        "shadow_trades",
        "provider_profile_pit_status IN ('resolved','legacy_unresolvable')",
    )
    op.create_check_constraint(
        "ck_shadow_trade_provider_profile_pit_provenance",
        "shadow_trades",
        "(provider_profile_pit_status='resolved' "
        "AND provider_profile_version_id IS NOT NULL "
        "AND provider_profile_version_no IS NOT NULL "
        "AND provider_profile_version_no > 0 "
        "AND provider_profile_effective_at IS NOT NULL) "
        "OR (provider_profile_pit_status='legacy_unresolvable' "
        "AND provider_profile_version_id IS NULL "
        "AND provider_profile_version_no IS NULL "
        "AND provider_profile_effective_at IS NULL)",
    )
    op.create_check_constraint(
        "ck_shadow_trade_score_requires_provider_pit",
        "shadow_trades",
        "NOT score_eligible OR provider_profile_pit_status='resolved'",
    )

    # The trigger validates the duplicated provenance columns against the
    # append-only ledger and prevents a future application path from attaching a
    # profile version that did not yet exist at signal time.
    op.execute(
        """
        CREATE FUNCTION enforce_shadow_trade_provider_profile_pit()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.provider_profile_pit_status = 'resolved' THEN
                PERFORM 1
                FROM provider_research_profile_versions v
                WHERE v.id = NEW.provider_profile_version_id
                  AND v.source_id = NEW.source_id
                  AND v.version_no = NEW.provider_profile_version_no
                  AND v.effective_at = NEW.provider_profile_effective_at
                  AND v.effective_at <= NEW.signal_posted_at;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'shadow trade provider profile PIT provenance invalid';
                END IF;
            ELSIF NEW.score_eligible THEN
                RAISE EXCEPTION 'legacy provider profile state cannot be score eligible';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_shadow_trade_provider_profile_pit
        BEFORE INSERT OR UPDATE OF
            source_id,signal_posted_at,provider_profile_version_id,
            provider_profile_version_no,provider_profile_effective_at,
            provider_profile_pit_status,score_eligible
        ON shadow_trades
        FOR EACH ROW EXECUTE FUNCTION enforce_shadow_trade_provider_profile_pit()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_shadow_trade_provider_profile_pit ON shadow_trades"
    )
    op.execute("DROP FUNCTION IF EXISTS enforce_shadow_trade_provider_profile_pit()")
    op.drop_constraint(
        "ck_shadow_trade_score_requires_provider_pit",
        "shadow_trades",
        type_="check",
    )
    op.drop_constraint(
        "ck_shadow_trade_provider_profile_pit_provenance",
        "shadow_trades",
        type_="check",
    )
    op.drop_constraint(
        "ck_shadow_trade_provider_profile_pit_status",
        "shadow_trades",
        type_="check",
    )
    op.drop_index(
        "ix_shadow_trades_provider_profile_version",
        table_name="shadow_trades",
    )
    op.drop_constraint(
        "fk_shadow_trade_provider_profile_version",
        "shadow_trades",
        type_="foreignkey",
    )
    op.drop_column("shadow_trades", "provider_profile_pit_status")
    op.drop_column("shadow_trades", "provider_profile_effective_at")
    op.drop_column("shadow_trades", "provider_profile_version_no")
    op.drop_column("shadow_trades", "provider_profile_version_id")
