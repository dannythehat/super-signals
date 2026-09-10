"""Backfill the verified missed 2026-09-10 XAUUSD SELL 4341 winner."""
from collections.abc import Sequence
from alembic import op
revision: str = "0072_backfill_xauusd_4341"
down_revision: str = "0071_merge_reporting_heads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
USER_ID = "ea604df2-f8ee-47d1-bc51-f0078dbf160d"
SIGNAL_ID = "ddfbaeb5-34de-4aa0-ab2f-693d7fec784e"
SOURCE_ID = "1f1f1310-fa03-4fb9-9044-636a1d4a8c21"
CLOSE_AT = "2026-09-10 12:40:00+00"
OPEN_AT = "2026-09-10 12:36:07.630226+00"
REASON = "reviewed_provider_result_all_3_tps_hit_double_lotsize"
def upgrade() -> None:
    op.execute(f"""
        UPDATE positions SET status='closed', entry_price=4341.00,
        exit_price=CASE tp_index WHEN 1 THEN 4337.00 WHEN 2 THEN 4336.00 WHEN 3 THEN 4310.00 END,
        volume=0.02000000, planned_risk_percent=1.00,
        pnl_amount=CASE tp_index WHEN 1 THEN 8.00 WHEN 2 THEN 10.00 WHEN 3 THEN 62.00 END,
        pnl_percent=CASE tp_index WHEN 1 THEN 0.555474 WHEN 2 THEN 0.694342 WHEN 3 THEN 4.304760 END,
        opened_at=COALESCE(opened_at,TIMESTAMPTZ '{OPEN_AT}'), closed_at=TIMESTAMPTZ '{CLOSE_AT}',
        close_reason='{REASON}', updated_at=now()
        WHERE signal_id=UUID '{SIGNAL_ID}' AND user_id=UUID '{USER_ID}';
    """)
    op.execute(f"""
        INSERT INTO performance_trade_outcomes
        (position_id,user_id,signal_id,source_id,symbol,side,status,opened_at,closed_at,entry_price,exit_price,volume,cash_pnl,return_percent,net_pips,pip_size,model_500_pnl,model_500_return_percent,planned_risk_percent,close_reason,broker_deal_count,source_digest,derived_at)
        SELECT p.id,p.user_id,p.signal_id,s.source_id,s.symbol,s.side,'won',p.opened_at,p.closed_at,p.entry_price,p.exit_price,p.volume,
        CASE p.tp_index WHEN 1 THEN 8.00 WHEN 2 THEN 10.00 WHEN 3 THEN 62.00 END,
        CASE p.tp_index WHEN 1 THEN 0.555474 WHEN 2 THEN 0.694342 WHEN 3 THEN 4.304760 END,
        CASE p.tp_index WHEN 1 THEN 40.00 WHEN 2 THEN 50.00 WHEN 3 THEN 310.00 END,0.1,
        CASE p.tp_index WHEN 1 THEN 0.16 WHEN 2 THEN 0.20 WHEN 3 THEN 1.25 END,
        CASE p.tp_index WHEN 1 THEN 0.032000 WHEN 2 THEN 0.040000 WHEN 3 THEN 0.250000 END,1.00,'{REASON}',0,
        md5((p.id::text || '_reviewed_provider_2026_09_10')::text),now()
        FROM positions p JOIN signals s ON s.id=p.signal_id
        WHERE p.signal_id=UUID '{SIGNAL_ID}' AND p.user_id=UUID '{USER_ID}'
        AND NOT EXISTS (SELECT 1 FROM performance_trade_outcomes o WHERE o.position_id=p.id);
    """)
    op.execute(f"""
        UPDATE performance_summaries SET total_trades=total_trades+1,wins=wins+1,
        cash_pnl=COALESCE(cash_pnl,0)+80.00,net_pips=COALESCE(net_pips,0)+400.00,
        gross_profit_pips=COALESCE(gross_profit_pips,0)+400.00,model_500_pnl=COALESCE(model_500_pnl,0)+1.61,
        model_500_return_percent=ROUND((COALESCE(model_500_pnl,0)+1.61)/500.0*100,6),generated_at=now()
        WHERE user_id=UUID '{USER_ID}' AND ((period_type='daily' AND period_start=TIMESTAMPTZ '2026-09-10 00:00:00+00') OR period_type='all_time')
        AND (dimension_type='portfolio' OR dimension_key='symbol:XAUUSD' OR dimension_key='source:{SOURCE_ID}');
    """)
def downgrade() -> None:
    op.execute(f"""DELETE FROM performance_trade_outcomes WHERE signal_id=UUID '{SIGNAL_ID}' AND close_reason='{REASON}';
    UPDATE positions SET status='error',exit_price=NULL,pnl_amount=NULL,pnl_percent=NULL,close_reason='day26_failed_metaapi_trade_rejected',closed_at=NULL,opened_at=NULL,volume=0.01000000,planned_risk_percent=1.00,updated_at=now()
    WHERE signal_id=UUID '{SIGNAL_ID}' AND user_id=UUID '{USER_ID}';""")