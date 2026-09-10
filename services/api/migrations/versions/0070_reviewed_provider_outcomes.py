"""Record the reviewed outcome for the 10 September XAUUSD signal.

The broker route failed before any broker deal was created, but the provider subsequently
published TP1, TP2 and TP3 as hit. Raw signals/positions remain untouched. These three
performance outcomes are explicitly marked as reviewed-provider outcomes and are
protected from the normal broker-derived rebuild so user-facing performance remains
accurate without fabricating broker deals.

Revision ID: 0070_reviewed_provider_outcomes
Revises: 0069_provider_day20_mgmt
Create Date: 2026-09-10
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0070_reviewed_provider_outcomes"
down_revision: str | None = "0069_provider_day20_mgmt"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

USER_ID = "cb783948-d8ef-466e-b70c-86bcd3832552"
SIGNAL_ID = "ddfbaeb5-34de-4aa0-ab2f-693d7fec784e"
SOURCE_ID = "1f1f1310-fa03-4fb9-9044-636a1d4a8c21"

LEGS = (
    ("f3bea2ef-6c82-451e-a213-01933f46686b", 1, "4337", "2026-09-10T12:37:28+00:00", 40, 8.00, "c4f9fe288999afb698b3fd49206ca285d135fc20a1bbd8b808be2df4089c6e88"),
    ("ed914223-24cd-49bf-9325-a74760d97675", 2, "4336", "2026-09-10T12:37:28+00:00", 50, 10.00, "e1c26b5ce5f045e7ff3ab146d8638e85ee104a9d7debc36484b624e28707f0df"),
    ("d87c9eed-4952-40f1-97c1-0322f67e2d18", 3, "4310", "2026-09-10T12:44:07+00:00", 310, 62.00, "92f0be3a7fac4a63a81ca33db8c0b797fbbf4dfe9630bc23bdf07ba5ce69edb0"),
)


def upgrade() -> None:
    op.create_table(
        "performance_reviewed_trade_overrides",
        sa.Column("position_id", sa.UUID(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("signal_id", sa.UUID(), sa.ForeignKey("signals.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("source_id", sa.UUID(), sa.ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_price", sa.Numeric(14, 4), nullable=False),
        sa.Column("exit_price", sa.Numeric(14, 4), nullable=False),
        sa.Column("volume", sa.Numeric(14, 8), nullable=False),
        sa.Column("cash_pnl", sa.Numeric(14, 2), nullable=False),
        sa.Column("net_pips", sa.Numeric(14, 4), nullable=False),
        sa.Column("pip_size", sa.Numeric(14, 6), nullable=False),
        sa.Column("planned_risk_percent", sa.Numeric(8, 4), nullable=False),
        sa.Column("close_reason", sa.String(120), nullable=False),
        sa.Column("source_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "signal_id", "position_id", name="uq_reviewed_provider_trade_override"),
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION preserve_reviewed_provider_outcome()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM performance_reviewed_trade_overrides
                WHERE position_id=OLD.position_id
            ) THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_preserve_reviewed_provider_outcome
        BEFORE UPDATE OR DELETE ON performance_trade_outcomes
        FOR EACH ROW EXECUTE FUNCTION preserve_reviewed_provider_outcome()
        """
    )

    op.execute(
        sa.text(
            """
            INSERT INTO performance_reviewed_trade_overrides (
                position_id,user_id,signal_id,source_id,status,opened_at,closed_at,
                entry_price,exit_price,volume,cash_pnl,net_pips,pip_size,
                planned_risk_percent,close_reason,source_digest
            ) VALUES
                (:p1,:user,:signal,:source,'won','2026-09-10T12:36:07+00:00','2026-09-10T12:37:28+00:00',4341,4337,0.02,8,40,0.1,1,'reviewed_provider_tp1_hit',:d1),
                (:p2,:user,:signal,:source,'won','2026-09-10T12:36:07+00:00','2026-09-10T12:37:28+00:00',4341,4336,0.02,10,50,0.1,1,'reviewed_provider_tp2_hit',:d2),
                (:p3,:user,:signal,:source,'won','2026-09-10T12:36:07+00:00','2026-09-10T12:44:07+00:00',4341,4310,0.02,62,310,0.1,1,'reviewed_provider_tp3_hit',:d3)
            ON CONFLICT (position_id) DO NOTHING
            """
        ).bindparams(
            p1=LEGS[0][0], p2=LEGS[1][0], p3=LEGS[2][0],
            user=USER_ID, signal=SIGNAL_ID, source=SOURCE_ID,
            d1=LEGS[0][6], d2=LEGS[1][6], d3=LEGS[2][6],
        )
    )

    op.execute(
        sa.text(
            """
            INSERT INTO performance_trade_outcomes (
                position_id,user_id,signal_id,source_id,trader_stream,symbol,side,status,
                opened_at,closed_at,entry_price,exit_price,volume,cash_pnl,return_percent,
                net_pips,pip_size,model_500_pnl,model_500_return_percent,planned_risk_percent,
                close_reason,broker_deal_count,source_digest,derived_at
            ) VALUES
                (:p1,:user,:signal,:source,NULL,'XAUUSD','SELL','won',
                 '2026-09-10T12:36:07+00:00','2026-09-10T12:37:28+00:00',4341,4337,0.02,8,NULL,
                 40,0.1,1.612903,0.322581,1,'reviewed_provider_tp1_hit',0,:d1,'2026-09-10T12:44:07+00:00'),
                (:p2,:user,:signal,:source,NULL,'XAUUSD','SELL','won',
                 '2026-09-10T12:36:07+00:00','2026-09-10T12:37:28+00:00',4341,4336,0.02,10,NULL,
                 50,0.1,2.016129,0.403226,1,'reviewed_provider_tp2_hit',0,:d2,'2026-09-10T12:44:07+00:00'),
                (:p3,:user,:signal,:source,NULL,'XAUUSD','SELL','won',
                 '2026-09-10T12:36:07+00:00','2026-09-10T12:44:07+00:00',4341,4310,0.02,62,NULL,
                 310,0.1,12.500000,2.500000,1,'reviewed_provider_tp3_hit',0,:d3,'2026-09-10T12:44:07+00:00')
            ON CONFLICT (position_id) DO UPDATE SET
                status=EXCLUDED.status,
                opened_at=EXCLUDED.opened_at,
                closed_at=EXCLUDED.closed_at,
                entry_price=EXCLUDED.entry_price,
                exit_price=EXCLUDED.exit_price,
                volume=EXCLUDED.volume,
                cash_pnl=EXCLUDED.cash_pnl,
                net_pips=EXCLUDED.net_pips,
                pip_size=EXCLUDED.pip_size,
                model_500_pnl=EXCLUDED.model_500_pnl,
                model_500_return_percent=EXCLUDED.model_500_return_percent,
                planned_risk_percent=EXCLUDED.planned_risk_percent,
                close_reason=EXCLUDED.close_reason,
                broker_deal_count=0,
                source_digest=EXCLUDED.source_digest,
                derived_at=EXCLUDED.derived_at
            """
        ).bindparams(
            p1=LEGS[0][0], p2=LEGS[1][0], p3=LEGS[2][0],
            user=USER_ID, signal=SIGNAL_ID, source=SOURCE_ID,
            d1=LEGS[0][6], d2=LEGS[1][6], d3=LEGS[2][6],
        )
    )

    op.execute(
        sa.text(
            """
            INSERT INTO audit_events (actor_user_id,event_type,entity_type,entity_id,payload)
            VALUES (
                :user,
                'performance.reviewed_provider_outcome_created',
                'signal',
                :signal,
                jsonb_build_object(
                    'provider_message_id',84904,
                    'tp1_hit_at','2026-09-10T12:37:28+00:00',
                    'tp2_hit_at','2026-09-10T12:37:28+00:00',
                    'tp3_hit_at','2026-09-10T12:44:07+00:00',
                    'double_lotsize',true,
                    'risk_percent',1,
                    'reviewed_cash_pnl',80,
                    'reviewed_net_pips',400,
                    'raw_broker_evidence_preserved',true
                )
            )
            """
        ).bindparams(user=USER_ID, signal=SIGNAL_ID)
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_preserve_reviewed_provider_outcome ON performance_trade_outcomes")
    op.execute("DROP FUNCTION IF EXISTS preserve_reviewed_provider_outcome()")
    op.drop_table("performance_reviewed_trade_overrides")
