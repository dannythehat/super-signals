"""Backfill verified missed FXTradingVision XAUUSD trades for 2026-09-11.

These messages were present in the raw Telegram ledger but were not promoted
into canonical signals because the semantic supervisor was unavailable. The
provider subsequently published explicit TP/SL results. This migration
reconstructs the user's 1% demo ledger without changing the risk policy.
"""
from collections.abc import Sequence
from alembic import op

revision: str = "0073_backfill_fxtradingvision_20260911"
down_revision: str = "0072_backfill_xauusd_4341"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

USER_ID = "ea604df2-f8ee-47d1-bc51-f0078dbf160d"
SOURCE_ID = "1f1f1310-fa03-4fb9-9044-636a1d4a8c21"
REASON = "reviewed_provider_result_2026_09_11"

# The first three new ideas were explicitly covered by the provider's
# "first gold sell positions are stopped out" message after the SL was moved
# to 4390. The following ten ideas each have explicit TP1/TP2 and TP3 hits.
TRADES = [
    (85010, "2026-09-11 12:36:26+00", 4336, 4332, 4331, 4300, 4365, 4390, "lost"),
    (85013, "2026-09-11 12:40:36+00", 4351, 4347, 4346, 4320, 4380, 4390, "lost"),
    (85018, "2026-09-11 12:52:32+00", 4376, 4372, 4371, 4330, 4400, 4390, "lost"),
    (85024, "2026-09-11 13:06:57+00", 4392, 4388, 4387, 4360, 4420, 4360, "won"),
    (85027, "2026-09-11 13:44:20+00", 4390, 4386, 4385, 4360, 4420, 4360, "won"),
    (85033, "2026-09-11 14:07:42+00", 4389, 4385, 4384, 4360, 4415, 4360, "won"),
    (85039, "2026-09-11 14:16:10+00", 4384, 4380, 4379, 4350, 4410, 4350, "won"),
    (85043, "2026-09-11 14:22:12+00", 4384, 4380, 4379, 4350, 4410, 4350, "won"),
    (85049, "2026-09-11 14:26:18+00", 4377, 4373, 4372, 4340, 4405, 4340, "won"),
    (85055, "2026-09-11 14:34:00+00", 4375, 4371, 4370, 4340, 4405, 4340, "won"),
    (85064, "2026-09-11 14:50:06+00", 4380, 4376, 4375, 4340, 4410, 4340, "won"),
    (85065, "2026-09-11 14:50:15+00", 4380, 4376, 4375, 4340, 4400, 4340, "won"),
    (85066, "2026-09-11 14:52:12+00", 4380, 4376, 4375, 4340, 4410, 4340, "won"),
]


def upgrade() -> None:
    for msg_id, posted_at, entry, tp1, tp2, tp3, sl, exit_price, signal_status in TRADES:
        op.execute(f"""
            INSERT INTO signals (
                source_message_id, source_id, provider_chat_id, provider_message_id,
                source_revision_index, source_posted_at, signal_fingerprint, symbol,
                side, order_type, entry_low, entry_high, stop_loss, take_profits,
                has_open_runner, parser_status, skip_reason, risk_multiplier, original_text
            )
            SELECT m.id, UUID '{SOURCE_ID}', -1001651583302, {msg_id}, 0, m.posted_at,
                md5(m.raw_text), 'XAUUSD', 'SELL', 'market', {entry}.0, {entry}.0,
                {sl}.0, '["{tp1}","{tp2}","{tp3}"]'::jsonb, false, 'accepted', NULL, 1.0, m.raw_text
            FROM messages m
            WHERE m.source_id=UUID '{SOURCE_ID}' AND m.telegram_message_id={msg_id}
              AND NOT EXISTS (
                SELECT 1 FROM signals s WHERE s.source_message_id=m.id
              );
        """)

        # Three provider-managed legs per signal. Double lot size was explicitly
        # stated in each TP1/TP2 result message, while the account risk policy
        # remains 1%; volume is therefore 0.02 as the reviewed-provider ledger
        # convention for these verified results.
        op.execute(f"""
            INSERT INTO positions (
                id,user_id,signal_id,status,symbol,side,entry_price,exit_price,
                volume,take_profit,stop_loss,planned_risk_percent,pnl_amount,pnl_percent,
                opened_at,closed_at,close_reason,created_at,updated_at,tp_index
            )
            SELECT gen_random_uuid(), UUID '{USER_ID}', s.id, 'closed', s.symbol, s.side,
                {entry}.0,
                CASE g.tp_index WHEN 1 THEN {tp1}.0 WHEN 2 THEN {tp2}.0 WHEN 3 THEN {tp3}.0 END,
                0.02000000,
                CASE g.tp_index WHEN 1 THEN {tp1}.0 WHEN 2 THEN {tp2}.0 WHEN 3 THEN {tp3}.0 END,
                {sl}.0, 1.0,
                ({entry}.0 - CASE g.tp_index WHEN 1 THEN {tp1}.0 WHEN 2 THEN {tp2}.0 WHEN 3 THEN {tp3}.0 END) * 2.0 * 100.0,
                (({entry}.0 - CASE g.tp_index WHEN 1 THEN {tp1}.0 WHEN 2 THEN {tp2}.0 WHEN 3 THEN {tp3}.0 END) * 2.0 * 100.0) / 500.0 * 100.0,
                TIMESTAMPTZ '{posted_at}',
                CASE WHEN '{signal_status}'='lost' THEN TIMESTAMPTZ '2026-09-11 13:02:25+00'
                     ELSE TIMESTAMPTZ '2026-09-11 16:23:50+00' END,
                '{REASON}', now(), now(), g.tp_index
            FROM signals s CROSS JOIN generate_series(1,3) g(tp_index)
            WHERE s.source_id=UUID '{SOURCE_ID}' AND s.provider_message_id={msg_id}
              AND NOT EXISTS (
                SELECT 1 FROM positions p WHERE p.signal_id=s.id AND p.user_id=UUID '{USER_ID}' AND p.tp_index=g.tp_index
              );
        """)

        # Replace the provisional TP3 exit for losses with the actual moved SL.
        if signal_status == "lost":
            op.execute(f"""
                UPDATE positions p SET exit_price=4390.0,
                    pnl_amount=({entry}.0-4390.0)*2.0*100.0,
                    pnl_percent=(({entry}.0-4390.0)*2.0*100.0)/500.0*100.0,
                    updated_at=now()
                FROM signals s
                WHERE p.signal_id=s.id AND s.provider_message_id={msg_id}
                  AND p.user_id=UUID '{USER_ID}';
            """)

        op.execute(f"""
            INSERT INTO performance_trade_outcomes (
                position_id,user_id,signal_id,source_id,symbol,side,status,opened_at,closed_at,
                entry_price,exit_price,volume,cash_pnl,return_percent,net_pips,pip_size,
                model_500_pnl,model_500_return_percent,planned_risk_percent,close_reason,
                broker_deal_count,source_digest,derived_at
            )
            SELECT p.id,p.user_id,p.signal_id,s.source_id,s.symbol,s.side,
                CASE WHEN '{signal_status}'='lost' THEN 'lost' ELSE 'won' END,
                p.opened_at,p.closed_at,p.entry_price,p.exit_price,p.volume,p.pnl_amount,
                p.pnl_percent,
                (p.entry_price-p.exit_price)*100.0,
                0.1,
                ((p.entry_price-p.exit_price)*100.0) / ABS((s.stop_loss-s.entry_low)*100.0) * 5.0,
                (((p.entry_price-p.exit_price)*100.0) / ABS((s.stop_loss-s.entry_low)*100.0) * 5.0) / 500.0 * 100.0,
                1.0,'{REASON}',0,md5((p.id::text || '_fxtradingvision_20260911')),now()
            FROM positions p JOIN signals s ON s.id=p.signal_id
            WHERE s.source_id=UUID '{SOURCE_ID}' AND s.provider_message_id={msg_id}
              AND p.user_id=UUID '{USER_ID}'
              AND NOT EXISTS (SELECT 1 FROM performance_trade_outcomes o WHERE o.position_id=p.id);
        """)


def downgrade() -> None:
    op.execute(f"""
        DELETE FROM performance_trade_outcomes WHERE close_reason='{REASON}';
        DELETE FROM positions WHERE close_reason='{REASON}' AND user_id=UUID '{USER_ID}';
        DELETE FROM signals WHERE source_id=UUID '{SOURCE_ID}' AND provider_message_id IN ({','.join(str(t[0]) for t in TRADES)});
    """)
