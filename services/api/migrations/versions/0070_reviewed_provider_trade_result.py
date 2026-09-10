"""Record a reviewed provider result when execution failed but the provider outcome is verified.

Revision ID: 0070_reviewed_provider_trade_result
Revises: 0069_provider_day20_mgmt
Create Date: 2026-09-10
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0070_reviewed_provider_trade_result"
down_revision: str | None = "0069_provider_day20_mgmt"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

USER_ID = "ea604df2-f8ee-47d1-bc51-f0078dbf160d"
SIGNAL_ID = "ddfbaeb5-34de-4aa0-ab2f-693d7fec784e"
SOURCE_ID = "1f1f1310-fa03-4fb9-9044-636a1d4a8c21"
CLOSE_AT = "2026-09-10 12:40:00+00"


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION preserve_reviewed_provider_outcome()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF OLD.close_reason LIKE 'reviewed_provider_result:%' THEN
                NEW.status := OLD.status;
                NEW.opened_at := OLD.opened_at;
                NEW.closed_at := OLD.closed_at;
                NEW.entry_price := OLD.entry_price;
                NEW.exit_price := OLD.exit_price;
                NEW.volume := OLD.volume;
                NEW.cash_pnl := OLD.cash_pnl;
                NEW.return_percent := OLD.return_percent;
                NEW.net_pips := OLD.net_pips;
                NEW.pip_size := OLD.pip_size;
                NEW.model_500_pnl := OLD.model_500_pnl;
                NEW.model_500_return_percent := OLD.model_500_return_percent;
                NEW.planned_risk_percent := OLD.planned_risk_percent;
                NEW.close_reason := OLD.close_reason;
                NEW.broker_deal_count := OLD.broker_deal_count;
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS preserve_reviewed_provider_outcome_trigger
        ON performance_trade_outcomes;
        CREATE TRIGGER preserve_reviewed_provider_outcome_trigger
        BEFORE UPDATE ON performance_trade_outcomes
        FOR EACH ROW
        EXECUTE FUNCTION preserve_reviewed_provider_outcome();
        """
    )
    op.execute(
        f"""
        UPDATE positions
        SET status='closed',
            entry_price=4341.00,
            exit_price=CASE tp_index WHEN 1 THEN 4337.00 WHEN 2 THEN 4336.00 WHEN 3 THEN 4310.00 END,
            volume=0.02000000,
            planned_risk_percent=1.00,
            pnl_amount=CASE tp_index WHEN 1 THEN 8.00 WHEN 2 THEN 10.00 WHEN 3 THEN 62.00 END,
            pnl_percent=CASE tp_index WHEN 1 THEN 0.555474 WHEN 2 THEN 0.694342 WHEN 3 THEN 4.304760 END,
            opened_at=COALESCE(opened_at,TIMESTAMPTZ '2026-09-10 12:36:07.630226+00'),
            closed_at=TIMESTAMPTZ '{CLOSE_AT}',
            close_reason='reviewed_provider_result:all_3_tps_hit;double_lotsize',
            updated_at=now()
        WHERE signal_id=UUID '{SIGNAL_ID}';

        UPDATE performance_trade_outcomes o
        SET status='won',
            opened_at=COALESCE(o.opened_at,TIMESTAMPTZ '2026-09-10 12:36:07.630226+00'),
            closed_at=TIMESTAMPTZ '{CLOSE_AT}',
            entry_price=4341.00,
            exit_price=CASE p.tp_index WHEN 1 THEN 4337.00 WHEN 2 THEN 4336.00 WHEN 3 THEN 4310.00 END,
            volume=0.02000000,
            cash_pnl=CASE p.tp_index WHEN 1 THEN 8.00 WHEN 2 THEN 10.00 WHEN 3 THEN 62.00 END,
            return_percent=CASE p.tp_index WHEN 1 THEN 0.555474 WHEN 2 THEN 0.694342 WHEN 3 THEN 4.304760 END,
            net_pips=CASE p.tp_index WHEN 1 THEN 40.00 WHEN 2 THEN 50.00 WHEN 3 THEN 310.00 END,
            pip_size=0.1,
            model_500_pnl=CASE p.tp_index WHEN 1 THEN 0.16 WHEN 2 THEN 0.20 WHEN 3 THEN 1.25 END,
            model_500_return_percent=CASE p.tp_index WHEN 1 THEN 0.032000 WHEN 2 THEN 0.040000 WHEN 3 THEN 0.250000 END,
            planned_risk_percent=1.00,
            close_reason='reviewed_provider_result:all_3_tps_hit;double_lotsize',
            broker_deal_count=0,
            derived_at=now()
        FROM positions p
        WHERE o.position_id=p.id
          AND p.signal_id=UUID '{SIGNAL_ID}';
        """
    )
    op.execute(
        f"""
        UPDATE performance_summaries
        SET total_trades=total_trades+3,
            wins=wins+3,
            cash_pnl=COALESCE(cash_pnl,0)+80.00,
            net_pips=COALESCE(net_pips,0)+400.00,
            gross_profit_pips=COALESCE(gross_profit_pips,0)+400.00,
            model_500_pnl=COALESCE(model_500_pnl,0)+1.61,
            model_500_return_percent=ROUND((COALESCE(model_500_pnl,0)+1.61)/500.0*100,6),
            return_percent=CASE
                WHEN period_type='all_time' THEN NULL
                ELSE ROUND(
                    (COALESCE(cash_pnl,0)+80.00)
                    / NULLIF((
                        SELECT s.balance
                        FROM performance_account_snapshots s
                        WHERE s.user_id=UUID '{USER_ID}'
                          AND s.captured_at<=performance_summaries.period_start
                        ORDER BY s.captured_at DESC
                        LIMIT 1
                    ),0) * 100,6)
            END,
            source_digest=md5((COALESCE(source_digest,'') || ':reviewed-provider-2026-09-10')::text),
            generated_at=now()
        WHERE user_id=UUID '{USER_ID}'
          AND period_start IN (
              TIMESTAMPTZ '2026-09-10 00:00:00+00',
              TIMESTAMPTZ '2026-09-07 00:00:00+00',
              TIMESTAMPTZ '2026-09-01 00:00:00+00',
              TIMESTAMPTZ '2026-01-01 00:00:00+00',
              TIMESTAMPTZ '1970-01-01 00:00:00+00'
          )
          AND (
              dimension_type='portfolio'
              OR dimension_key='symbol:XAUUSD'
              OR dimension_key='source:{SOURCE_ID}'
          );
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS preserve_reviewed_provider_outcome_trigger
        ON performance_trade_outcomes;
        DROP FUNCTION IF EXISTS preserve_reviewed_provider_outcome();
        """
    )
