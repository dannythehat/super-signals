"""Add PIT-safe provider execution-cost and paper-to-broker calibration surfaces.

Revision ID: 0059_provider_execution_calibration
Revises: 0058_provider_aidy_context
Create Date: 2026-09-06

The views are research-only. They read immutable broker history and existing shadow
research; they do not change execution, risk, provider status, or broker authority.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0059_provider_execution_calibration"
down_revision: str | None = "0058_provider_aidy_context"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        r"""
        CREATE VIEW provider_execution_calibration_samples AS
        WITH entry_audit AS (
            SELECT DISTINCT ON (entity_id)
                   entity_id AS signal_id,
                   CASE
                       WHEN payload->>'execution_entry' ~ '^[0-9]+(?:\.[0-9]+)?$'
                       THEN (payload->>'execution_entry')::numeric
                       ELSE NULL
                   END AS entry_reference_price,
                   created_at AS entry_reference_at
            FROM audit_events
            WHERE entity_type='signal'
              AND event_type='mt5.day26_execution_success'
              AND payload ? 'execution_entry'
            ORDER BY entity_id,created_at ASC
        ),
        deal_rollup AS (
            SELECT
                position_id,
                signal_id,
                source_id,
                mt5_account_id,
                SUM(price*volume) FILTER (
                    WHERE entry_type='DEAL_ENTRY_IN' AND price IS NOT NULL AND volume>0
                ) / NULLIF(SUM(volume) FILTER (
                    WHERE entry_type='DEAL_ENTRY_IN' AND price IS NOT NULL AND volume>0
                ),0) AS broker_entry_vwap,
                SUM(volume) FILTER (
                    WHERE entry_type='DEAL_ENTRY_IN' AND price IS NOT NULL AND volume>0
                ) AS broker_entry_volume,
                SUM(price*volume) FILTER (
                    WHERE entry_type='DEAL_ENTRY_OUT' AND price IS NOT NULL AND volume>0
                ) / NULLIF(SUM(volume) FILTER (
                    WHERE entry_type='DEAL_ENTRY_OUT' AND price IS NOT NULL AND volume>0
                ),0) AS broker_exit_vwap,
                SUM(volume) FILTER (
                    WHERE entry_type='DEAL_ENTRY_OUT' AND price IS NOT NULL AND volume>0
                ) AS broker_exit_volume,
                SUM(profit) AS gross_profit_usd,
                SUM(commission) AS commission_usd,
                SUM(swap) AS swap_usd,
                COUNT(*) AS broker_deal_count,
                MIN(occurred_at) AS first_broker_deal_at,
                MAX(occurred_at) AS last_broker_deal_at
            FROM broker_deals
            WHERE position_id IS NOT NULL
              AND signal_id IS NOT NULL
              AND source_id IS NOT NULL
              AND UPPER(COALESCE(symbol,''))='XAUUSD'
            GROUP BY position_id,signal_id,source_id,mt5_account_id
        ),
        base AS (
            SELECT
                p.id AS position_id,
                p.signal_id,
                s.source_id,
                d.mt5_account_id,
                a.account_environment,
                src.status AS source_status_current,
                s.side,
                p.entry_order_type,
                p.entry_index,
                p.tp_index,
                s.source_posted_at AS signal_posted_at,
                p.opened_at,
                p.closed_at,
                p.close_reason,
                p.take_profit,
                p.stop_loss,
                ea.entry_reference_price,
                ea.entry_reference_at,
                d.broker_entry_vwap,
                d.broker_entry_volume,
                d.broker_exit_vwap,
                d.broker_exit_volume,
                d.gross_profit_usd,
                d.commission_usd,
                d.swap_usd,
                d.broker_deal_count,
                d.first_broker_deal_at,
                d.last_broker_deal_at
            FROM positions p
            JOIN signals s ON s.id=p.signal_id
            JOIN sources src ON src.id=s.source_id
            JOIN deal_rollup d
              ON d.position_id=p.id
             AND d.signal_id=p.signal_id
             AND d.source_id=s.source_id
            JOIN mt5_accounts a ON a.id=d.mt5_account_id
            LEFT JOIN entry_audit ea ON ea.signal_id=p.signal_id
            WHERE p.status='closed'
              AND p.closed_at IS NOT NULL
              AND UPPER(COALESCE(s.symbol,''))='XAUUSD'
              AND s.side IN ('BUY','SELL')
        ),
        classified AS (
            SELECT
                b.*,
                CASE
                    WHEN b.close_reason IN ('external_close','broker_settled')
                     AND b.broker_exit_vwap IS NOT NULL
                     AND b.take_profit IS NOT NULL
                     AND ABS(b.broker_exit_vwap-b.take_profit)<=0.75
                     AND (
                         b.stop_loss IS NULL
                         OR ABS(b.broker_exit_vwap-b.stop_loss)>0.75
                         OR ABS(b.broker_exit_vwap-b.take_profit)
                            <=ABS(b.broker_exit_vwap-b.stop_loss)
                     )
                    THEN 'take_profit'
                    WHEN b.close_reason IN ('external_close','broker_settled')
                     AND b.broker_exit_vwap IS NOT NULL
                     AND b.stop_loss IS NOT NULL
                     AND ABS(b.broker_exit_vwap-b.stop_loss)<=0.75
                    THEN 'stop_loss'
                    ELSE NULL
                END AS exit_reference_type,
                CASE
                    WHEN b.close_reason IN ('external_close','broker_settled')
                     AND b.broker_exit_vwap IS NOT NULL
                     AND b.take_profit IS NOT NULL
                     AND ABS(b.broker_exit_vwap-b.take_profit)<=0.75
                     AND (
                         b.stop_loss IS NULL
                         OR ABS(b.broker_exit_vwap-b.stop_loss)>0.75
                         OR ABS(b.broker_exit_vwap-b.take_profit)
                            <=ABS(b.broker_exit_vwap-b.stop_loss)
                     )
                    THEN b.take_profit
                    WHEN b.close_reason IN ('external_close','broker_settled')
                     AND b.broker_exit_vwap IS NOT NULL
                     AND b.stop_loss IS NOT NULL
                     AND ABS(b.broker_exit_vwap-b.stop_loss)<=0.75
                    THEN b.stop_loss
                    ELSE NULL
                END AS exit_reference_price
            FROM base b
        )
        SELECT
            position_id,
            signal_id,
            source_id,
            mt5_account_id,
            account_environment,
            source_status_current,
            side,
            entry_order_type,
            entry_index,
            tp_index,
            signal_posted_at,
            opened_at,
            closed_at,
            close_reason,
            entry_reference_price,
            CASE WHEN entry_reference_price IS NOT NULL
                 THEN 'pre_submit_executable_quote'
                 ELSE 'unavailable_legacy_reference'
            END AS entry_reference_basis,
            entry_reference_at,
            broker_entry_vwap,
            CASE
                WHEN entry_reference_price IS NULL OR broker_entry_vwap IS NULL THEN NULL
                WHEN side='BUY' THEN broker_entry_vwap-entry_reference_price
                ELSE entry_reference_price-broker_entry_vwap
            END AS entry_adverse_slippage_points,
            exit_reference_type,
            exit_reference_price,
            broker_exit_vwap,
            CASE
                WHEN exit_reference_price IS NULL OR broker_exit_vwap IS NULL THEN NULL
                WHEN side='BUY' THEN exit_reference_price-broker_exit_vwap
                ELSE broker_exit_vwap-exit_reference_price
            END AS exit_adverse_slippage_points,
            broker_entry_volume,
            broker_exit_volume,
            gross_profit_usd,
            commission_usd,
            swap_usd,
            gross_profit_usd+commission_usd+swap_usd AS net_profit_usd,
            -(commission_usd+swap_usd) AS broker_cash_charge_usd,
            CASE
                WHEN broker_entry_volume>0
                THEN -(commission_usd+swap_usd)/broker_entry_volume
                ELSE NULL
            END AS broker_cash_charge_usd_per_lot,
            CASE
                WHEN broker_entry_vwap IS NOT NULL
                 AND broker_exit_vwap IS NOT NULL
                 AND broker_entry_volume>0
                 AND gross_profit_usd<>0
                 AND ABS(broker_exit_vwap-broker_entry_vwap)>0
                THEN ABS(gross_profit_usd) /
                     (ABS(broker_exit_vwap-broker_entry_vwap)*broker_entry_volume)
                ELSE NULL
            END AS implied_usd_per_point_per_lot,
            broker_deal_count,
            first_broker_deal_at,
            last_broker_deal_at,
            'direct_broker'::text AS calibration_evidence,
            'shadow_bid_ask_already_included_do_not_double_count'::text AS spread_handling,
            true AS research_only,
            false AS live_money_execution_allowed
        FROM classified
        """
    )

    op.execute(
        r"""
        CREATE VIEW provider_execution_cost_model AS
        SELECT
            account_environment,
            COUNT(*) AS broker_position_samples,
            COUNT(entry_adverse_slippage_points) AS entry_slippage_samples,
            percentile_cont(0.50) WITHIN GROUP (
                ORDER BY entry_adverse_slippage_points
            )::numeric AS entry_adverse_p50_points,
            percentile_cont(0.95) WITHIN GROUP (
                ORDER BY entry_adverse_slippage_points
            )::numeric AS entry_adverse_p95_points,
            COUNT(exit_adverse_slippage_points) AS exit_slippage_samples,
            percentile_cont(0.50) WITHIN GROUP (
                ORDER BY exit_adverse_slippage_points
            )::numeric AS exit_adverse_p50_points,
            percentile_cont(0.95) WITHIN GROUP (
                ORDER BY exit_adverse_slippage_points
            )::numeric AS exit_adverse_p95_points,
            COUNT(implied_usd_per_point_per_lot) FILTER (
                WHERE implied_usd_per_point_per_lot>0
                  AND implied_usd_per_point_per_lot<1000
            ) AS contract_value_samples,
            percentile_cont(0.50) WITHIN GROUP (
                ORDER BY implied_usd_per_point_per_lot
            ) FILTER (
                WHERE implied_usd_per_point_per_lot>0
                  AND implied_usd_per_point_per_lot<1000
            )::numeric AS usd_per_point_per_lot_p50,
            COUNT(broker_cash_charge_usd_per_lot) FILTER (
                WHERE broker_cash_charge_usd_per_lot IS NOT NULL
            ) AS cash_charge_samples,
            percentile_cont(0.50) WITHIN GROUP (
                ORDER BY GREATEST(broker_cash_charge_usd_per_lot,0)
            ) FILTER (
                WHERE broker_cash_charge_usd_per_lot IS NOT NULL
            )::numeric AS cash_charge_per_lot_p50_usd,
            percentile_cont(0.95) WITHIN GROUP (
                ORDER BY GREATEST(broker_cash_charge_usd_per_lot,0)
            ) FILTER (
                WHERE broker_cash_charge_usd_per_lot IS NOT NULL
            )::numeric AS cash_charge_per_lot_p95_usd,
            SUM(commission_usd) AS observed_commission_usd,
            SUM(swap_usd) AS observed_swap_usd,
            MAX(closed_at) AS model_as_of_utc,
            CASE
                WHEN COUNT(entry_adverse_slippage_points)>=30
                 AND COUNT(exit_adverse_slippage_points)>=30
                 AND COUNT(implied_usd_per_point_per_lot) FILTER (
                     WHERE implied_usd_per_point_per_lot>0
                       AND implied_usd_per_point_per_lot<1000
                 )>=30
                 AND COUNT(broker_cash_charge_usd_per_lot) FILTER (
                     WHERE broker_cash_charge_usd_per_lot IS NOT NULL
                 )>=30
                THEN 'ENGINEERING_CALIBRATED'
                ELSE 'WAITING_INSUFFICIENT_SAMPLES'
            END AS model_status,
            true AS research_only,
            false AS live_money_execution_allowed
        FROM provider_execution_calibration_samples
        GROUP BY account_environment
        """
    )

    op.execute(
        r"""
        CREATE VIEW provider_shadow_execution_projection AS
        WITH leg_counts AS (
            SELECT
                shadow_trade_id,
                COUNT(*) FILTER (WHERE opened_at IS NOT NULL) AS executed_leg_count,
                COUNT(*) FILTER (
                    WHERE opened_at IS NOT NULL
                      AND status='closed'
                      AND exit_reason IN ('target','shadow_stop')
                ) AS barrier_exit_count
            FROM shadow_trade_legs
            GROUP BY shadow_trade_id
        ),
        modeled AS (
            SELECT
                t.id AS shadow_trade_id,
                t.source_id,
                t.signal_id,
                t.signal_posted_at,
                t.provider_profile_version_id,
                t.provider_profile_version_no,
                a.aidy_context_as_of_utc,
                t.side,
                t.entry_order_type,
                t.entry_price,
                t.initial_stop,
                ABS(t.entry_price-t.initial_stop) AS risk_distance_points,
                t.quality_r_multiple AS spread_aware_quality_r,
                t.benchmark_pnl_usd AS spread_aware_benchmark_pnl_usd,
                t.benchmark_risk_per_leg_usd,
                COALESCE(l.executed_leg_count,0) AS executed_leg_count,
                COALESCE(l.barrier_exit_count,0) AS barrier_exit_count,
                GREATEST(COALESCE(l.executed_leg_count,0)-COALESCE(l.barrier_exit_count,0),0)
                    AS unmodeled_exit_count,
                m.entry_samples,
                m.entry_p50,
                m.entry_p95,
                m.exit_samples,
                m.exit_p50,
                m.exit_p95,
                m.contract_samples,
                m.contract_p50,
                m.charge_samples,
                m.charge_p50,
                m.charge_p95,
                m.latest_sample_closed_at
            FROM shadow_trades t
            JOIN provider_signal_context_attachments a ON a.signal_id=t.signal_id
            LEFT JOIN leg_counts l ON l.shadow_trade_id=t.id
            LEFT JOIN LATERAL (
                SELECT
                    COUNT(c.entry_adverse_slippage_points) AS entry_samples,
                    GREATEST(
                        percentile_cont(0.50) WITHIN GROUP (
                            ORDER BY c.entry_adverse_slippage_points
                        )::numeric,
                        0
                    ) AS entry_p50,
                    GREATEST(
                        percentile_cont(0.95) WITHIN GROUP (
                            ORDER BY c.entry_adverse_slippage_points
                        )::numeric,
                        0
                    ) AS entry_p95,
                    COUNT(c.exit_adverse_slippage_points) AS exit_samples,
                    GREATEST(
                        percentile_cont(0.50) WITHIN GROUP (
                            ORDER BY c.exit_adverse_slippage_points
                        )::numeric,
                        0
                    ) AS exit_p50,
                    GREATEST(
                        percentile_cont(0.95) WITHIN GROUP (
                            ORDER BY c.exit_adverse_slippage_points
                        )::numeric,
                        0
                    ) AS exit_p95,
                    COUNT(c.implied_usd_per_point_per_lot) FILTER (
                        WHERE c.implied_usd_per_point_per_lot>0
                          AND c.implied_usd_per_point_per_lot<1000
                    ) AS contract_samples,
                    percentile_cont(0.50) WITHIN GROUP (
                        ORDER BY c.implied_usd_per_point_per_lot
                    ) FILTER (
                        WHERE c.implied_usd_per_point_per_lot>0
                          AND c.implied_usd_per_point_per_lot<1000
                    )::numeric AS contract_p50,
                    COUNT(c.broker_cash_charge_usd_per_lot) FILTER (
                        WHERE c.broker_cash_charge_usd_per_lot IS NOT NULL
                    ) AS charge_samples,
                    percentile_cont(0.50) WITHIN GROUP (
                        ORDER BY GREATEST(c.broker_cash_charge_usd_per_lot,0)
                    ) FILTER (
                        WHERE c.broker_cash_charge_usd_per_lot IS NOT NULL
                    )::numeric AS charge_p50,
                    percentile_cont(0.95) WITHIN GROUP (
                        ORDER BY GREATEST(c.broker_cash_charge_usd_per_lot,0)
                    ) FILTER (
                        WHERE c.broker_cash_charge_usd_per_lot IS NOT NULL
                    )::numeric AS charge_p95,
                    MAX(c.closed_at) AS latest_sample_closed_at
                FROM provider_execution_calibration_samples c
                WHERE c.account_environment='demo'
                  AND c.closed_at<=t.signal_posted_at
            ) m ON true
            WHERE t.status='closed'
              AND t.score_eligible=true
              AND t.aidy_score_blocked=false
              AND t.provider_profile_pit_status='resolved'
              AND t.provider_profile_version_id IS NOT NULL
              AND t.entry_price IS NOT NULL
              AND t.initial_stop IS NOT NULL
        ),
        costed AS (
            SELECT
                m.*,
                CASE
                    WHEN risk_distance_points<=0 THEN 'WAITING_INVALID_RISK_DISTANCE'
                    WHEN entry_samples<30 THEN 'WAITING_INSUFFICIENT_ENTRY_SAMPLES'
                    WHEN barrier_exit_count>0 AND exit_samples<30
                        THEN 'WAITING_INSUFFICIENT_EXIT_SAMPLES'
                    WHEN contract_samples<30 THEN 'WAITING_INSUFFICIENT_CONTRACT_SAMPLES'
                    WHEN charge_samples<30 THEN 'WAITING_INSUFFICIENT_CASH_COST_SAMPLES'
                    WHEN unmodeled_exit_count>0 THEN 'ENGINEERING_CALIBRATED_PARTIAL_EXIT_MODEL'
                    ELSE 'ENGINEERING_CALIBRATED'
                END AS projection_status,
                CASE WHEN risk_distance_points>0 AND contract_p50>0
                     THEN benchmark_risk_per_leg_usd/(risk_distance_points*contract_p50)
                     ELSE NULL END AS modeled_volume_per_leg,
                CASE WHEN risk_distance_points>0
                     THEN (
                         executed_leg_count*COALESCE(entry_p50,0)
                         + barrier_exit_count*COALESCE(exit_p50,0)
                     )/risk_distance_points
                     ELSE NULL END AS slippage_cost_r_p50,
                CASE WHEN risk_distance_points>0
                     THEN (
                         executed_leg_count*COALESCE(entry_p95,0)
                         + barrier_exit_count*COALESCE(exit_p95,0)
                     )/risk_distance_points
                     ELSE NULL END AS slippage_cost_r_p95
            FROM modeled m
        ),
        projected AS (
            SELECT
                c.*,
                CASE WHEN modeled_volume_per_leg IS NOT NULL AND benchmark_risk_per_leg_usd>0
                     THEN (
                         executed_leg_count*modeled_volume_per_leg*COALESCE(charge_p50,0)
                     )/benchmark_risk_per_leg_usd
                     ELSE NULL END AS cash_charge_r_p50,
                CASE WHEN modeled_volume_per_leg IS NOT NULL AND benchmark_risk_per_leg_usd>0
                     THEN (
                         executed_leg_count*modeled_volume_per_leg*COALESCE(charge_p95,0)
                     )/benchmark_risk_per_leg_usd
                     ELSE NULL END AS cash_charge_r_p95
            FROM costed c
        )
        SELECT
            shadow_trade_id,
            source_id,
            signal_id,
            signal_posted_at,
            provider_profile_version_id,
            provider_profile_version_no,
            aidy_context_as_of_utc,
            side,
            entry_order_type,
            entry_price,
            initial_stop,
            risk_distance_points,
            executed_leg_count,
            barrier_exit_count,
            unmodeled_exit_count,
            spread_aware_quality_r,
            spread_aware_benchmark_pnl_usd,
            benchmark_risk_per_leg_usd,
            entry_samples,
            entry_p50 AS entry_adverse_p50_points,
            entry_p95 AS entry_adverse_p95_points,
            exit_samples,
            exit_p50 AS exit_adverse_p50_points,
            exit_p95 AS exit_adverse_p95_points,
            contract_samples,
            contract_p50 AS usd_per_point_per_lot_p50,
            charge_samples,
            charge_p50 AS cash_charge_per_lot_p50_usd,
            charge_p95 AS cash_charge_per_lot_p95_usd,
            latest_sample_closed_at AS calibration_evidence_as_of_utc,
            modeled_volume_per_leg,
            slippage_cost_r_p50,
            slippage_cost_r_p95,
            cash_charge_r_p50,
            cash_charge_r_p95,
            CASE WHEN projection_status LIKE 'ENGINEERING_CALIBRATED%'
                 THEN spread_aware_quality_r
                      - COALESCE(slippage_cost_r_p50,0)
                      - COALESCE(cash_charge_r_p50,0)
                 ELSE NULL END AS execution_adjusted_r_p50,
            CASE WHEN projection_status LIKE 'ENGINEERING_CALIBRATED%'
                 THEN spread_aware_quality_r
                      - COALESCE(slippage_cost_r_p95,0)
                      - COALESCE(cash_charge_r_p95,0)
                 ELSE NULL END AS execution_adjusted_r_p95,
            CASE WHEN projection_status LIKE 'ENGINEERING_CALIBRATED%'
                 THEN (
                     spread_aware_quality_r
                     - COALESCE(slippage_cost_r_p50,0)
                     - COALESCE(cash_charge_r_p50,0)
                 )*benchmark_risk_per_leg_usd
                 ELSE NULL END AS execution_adjusted_benchmark_pnl_usd_p50,
            CASE WHEN projection_status LIKE 'ENGINEERING_CALIBRATED%'
                 THEN (
                     spread_aware_quality_r
                     - COALESCE(slippage_cost_r_p95,0)
                     - COALESCE(cash_charge_r_p95,0)
                 )*benchmark_risk_per_leg_usd
                 ELSE NULL END AS execution_adjusted_benchmark_pnl_usd_p95,
            projection_status,
            'demo'::text AS calibration_account_environment,
            'shadow_bid_ask_spread_already_included'::text AS spread_handling,
            'closed_at_lte_signal_posted_at'::text AS point_in_time_calibration_rule,
            true AS research_only,
            false AS live_money_execution_allowed
        FROM projected
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS provider_shadow_execution_projection")
    op.execute("DROP VIEW IF EXISTS provider_execution_cost_model")
    op.execute("DROP VIEW IF EXISTS provider_execution_calibration_samples")
