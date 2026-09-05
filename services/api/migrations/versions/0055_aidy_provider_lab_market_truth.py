"""Make AIDY M1 deterministic replay the canonical intraday/swing Provider Lab truth.

Revision ID: 0055_aidy_provider_lab_truth
Revises: 0054_pause_member_access
Create Date: 2026-09-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0055_aidy_provider_lab_truth"
down_revision: str | None = "0054_pause_member_access"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_shadow_fair_quote_mode", "shadow_trades", type_="check")
    op.create_check_constraint(
        "ck_shadow_fair_quote_mode",
        "shadow_trades",
        "quote_mode IN ('unobserved','snapshot_poll','stream_quote','stream_tick','aidy_m1')",
    )
    op.add_column("shadow_trades", sa.Column("aidy_m1_cursor_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("shadow_trades", sa.Column("aidy_effective_stop", sa.Numeric(24, 10), nullable=True))
    op.add_column("shadow_trades", sa.Column("aidy_resolution_note", sa.String(length=160), nullable=True))
    op.add_column("shadow_trades", sa.Column("aidy_state_version", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("shadow_trades", sa.Column("aidy_lifecycle_watermark", sa.String(length=64), nullable=True))
    op.add_column("shadow_trades", sa.Column("aidy_terminal", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("shadow_trades", sa.Column("aidy_score_blocked", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column(
        "shadow_trades",
        sa.Column(
            "aidy_original_geometry",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column("shadow_trades", sa.Column("aidy_evidence_digest", sa.String(length=64), nullable=True))
    op.add_column(
        "shadow_trades",
        sa.Column(
            "aidy_resolution_evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column("shadow_trade_legs", sa.Column("aidy_original_target", sa.Numeric(24, 10), nullable=True))
    op.add_column("shadow_trade_legs", sa.Column("aidy_effective_target", sa.Numeric(24, 10), nullable=True))

    op.execute(
        """
        UPDATE shadow_trade_legs l
        SET aidy_original_target = CASE
                WHEN l.is_runner THEN NULL
                ELSE NULLIF(t.take_profits ->> (l.tp_index - 1), '')::numeric
            END,
            aidy_effective_target = CASE
                WHEN l.is_runner THEN NULL
                ELSE NULLIF(t.take_profits ->> (l.tp_index - 1), '')::numeric
            END
        FROM shadow_trades t
        WHERE t.id=l.shadow_trade_id
          AND t.provider_style IN ('intraday','swing_or_sparse')
        """
    )
    op.execute(
        """
        UPDATE shadow_trades t
        SET aidy_original_geometry = jsonb_build_object(
                'side',t.side,
                'entry_order_type',t.entry_order_type,
                'entry_low',t.entry_low::text,
                'entry_high',t.entry_high::text,
                'initial_stop',t.initial_stop::text,
                'entry_index',t.entry_index,
                'legs',COALESCE((
                    SELECT jsonb_agg(
                        jsonb_build_object(
                            'tp_index',l.tp_index,
                            'target_price',CASE WHEN l.is_runner THEN NULL ELSE l.aidy_original_target::text END,
                            'is_runner',l.is_runner
                        ) ORDER BY l.tp_index
                    )
                    FROM shadow_trade_legs l WHERE l.shadow_trade_id=t.id
                ),'[]'::jsonb)
            ),
            aidy_effective_stop=t.initial_stop
        WHERE t.provider_style IN ('intraday','swing_or_sparse')
        """
    )

    op.execute(
        """
        UPDATE shadow_trades
        SET score_eligible=false,
            score_exclusion_reason='aidy_m1_revalidation_required',
            aidy_m1_cursor_at=NULL,
            aidy_lifecycle_watermark=NULL,
            aidy_state_version=0,
            aidy_terminal=false,
            aidy_score_blocked=false,
            aidy_evidence_digest=NULL,
            aidy_resolution_evidence='[]'::jsonb,
            aidy_resolution_note=NULL,
            aidy_effective_stop=initial_stop,
            updated_at=now()
        WHERE provider_style IN ('intraday','swing_or_sparse')
          AND quote_mode<>'aidy_m1'
        """
    )
    op.execute(
        """
        UPDATE shadow_trades
        SET score_eligible=false,
            score_exclusion_reason='unsupported_style_scalper',
            updated_at=now()
        WHERE provider_style='scalper'
        """
    )

    op.create_check_constraint(
        "ck_shadow_aidy_score_mode",
        "shadow_trades",
        "NOT score_eligible OR provider_style NOT IN ('intraday','swing_or_sparse') OR quote_mode='aidy_m1'",
    )
    op.create_check_constraint(
        "ck_shadow_aidy_state_version",
        "shadow_trades",
        "aidy_state_version >= 0",
    )

    op.execute("ALTER VIEW provider_benchmark_performance RENAME TO provider_benchmark_performance_pre_aidy_0055")
    op.execute(
        """
        CREATE VIEW provider_benchmark_performance AS
        SELECT p.*
        FROM provider_benchmark_performance_pre_aidy_0055 p
        WHERE NOT EXISTS (
            SELECT 1 FROM shadow_trades t
            WHERE t.source_id=p.source_id
              AND t.score_eligible
              AND t.provider_style IN ('intraday','swing_or_sparse')
              AND t.quote_mode<>'aidy_m1'
        )
        """
    )
    op.execute("ALTER VIEW provider_benchmark_segments RENAME TO provider_benchmark_segments_pre_aidy_0055")
    op.execute(
        """
        CREATE VIEW provider_benchmark_segments AS
        SELECT p.*
        FROM provider_benchmark_segments_pre_aidy_0055 p
        WHERE NOT EXISTS (
            SELECT 1 FROM shadow_trades t
            WHERE t.source_id=p.source_id
              AND t.score_eligible
              AND t.provider_style IN ('intraday','swing_or_sparse')
              AND t.quote_mode<>'aidy_m1'
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS provider_benchmark_segments")
    op.execute("ALTER VIEW provider_benchmark_segments_pre_aidy_0055 RENAME TO provider_benchmark_segments")
    op.execute("DROP VIEW IF EXISTS provider_benchmark_performance")
    op.execute("ALTER VIEW provider_benchmark_performance_pre_aidy_0055 RENAME TO provider_benchmark_performance")
    op.drop_constraint("ck_shadow_aidy_state_version", "shadow_trades", type_="check")
    op.drop_constraint("ck_shadow_aidy_score_mode", "shadow_trades", type_="check")
    op.execute(
        """
        UPDATE shadow_trades
        SET quote_mode='unobserved',
            score_eligible=false,
            score_exclusion_reason='market_data_not_observed',
            updated_at=now()
        WHERE quote_mode='aidy_m1'
        """
    )
    op.drop_column("shadow_trade_legs", "aidy_effective_target")
    op.drop_column("shadow_trade_legs", "aidy_original_target")
    for column in (
        "aidy_resolution_evidence",
        "aidy_evidence_digest",
        "aidy_original_geometry",
        "aidy_score_blocked",
        "aidy_terminal",
        "aidy_lifecycle_watermark",
        "aidy_state_version",
        "aidy_resolution_note",
        "aidy_effective_stop",
        "aidy_m1_cursor_at",
    ):
        op.drop_column("shadow_trades", column)
    op.drop_constraint("ck_shadow_fair_quote_mode", "shadow_trades", type_="check")
    op.create_check_constraint(
        "ck_shadow_fair_quote_mode",
        "shadow_trades",
        "quote_mode IN ('unobserved','snapshot_poll','stream_quote','stream_tick')",
    )
