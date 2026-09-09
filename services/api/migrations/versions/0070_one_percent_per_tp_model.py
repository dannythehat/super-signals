"""Normalize the $500 performance model to stored per-TP risk.

Revision ID: 0070_one_percent_per_tp_model
Revises: 0069_provider_day20_mgmt
Create Date: 2026-09-09

The execution engine stores planned risk on each broker position. Reporting must use that
stored per-leg truth rather than provider wording such as DOUBLE LOT. Four 1% TP/runner
legs therefore model as 4% total planned risk, never 8% because a signal has two entry
sections or a risk_multiplier of 2.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0070_one_percent_per_tp_model"
down_revision: str | None = "0069_provider_day20_mgmt"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION normalize_model_500_from_planned_risk()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_stop numeric;
            v_move numeric;
            v_risk_distance numeric;
        BEGIN
            IF NEW.status IN ('won','lost','breakeven')
               AND NEW.entry_price IS NOT NULL
               AND NEW.exit_price IS NOT NULL
               AND NEW.planned_risk_percent IS NOT NULL THEN
                SELECT COALESCE(p.stop_loss, s.stop_loss)
                  INTO v_stop
                  FROM positions AS p
                  JOIN signals AS s ON s.id = p.signal_id
                 WHERE p.id = NEW.position_id;

                IF v_stop IS NOT NULL THEN
                    v_risk_distance := abs(NEW.entry_price - v_stop);
                    IF v_risk_distance > 0 THEN
                        v_move := CASE
                            WHEN upper(NEW.side) = 'BUY'
                                THEN NEW.exit_price - NEW.entry_price
                            WHEN upper(NEW.side) = 'SELL'
                                THEN NEW.entry_price - NEW.exit_price
                            ELSE NULL
                        END;
                        IF v_move IS NOT NULL THEN
                            NEW.model_500_pnl := round(
                                500::numeric
                                * (NEW.planned_risk_percent / 100::numeric)
                                * (v_move / v_risk_distance),
                                2
                            );
                            NEW.model_500_return_percent := round(
                                (NEW.model_500_pnl / 500::numeric) * 100::numeric,
                                6
                            );
                        END IF;
                    END IF;
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_normalize_model_500_from_planned_risk "
        "ON performance_trade_outcomes"
    )
    op.execute(
        """
        CREATE TRIGGER trg_normalize_model_500_from_planned_risk
        BEFORE INSERT OR UPDATE ON performance_trade_outcomes
        FOR EACH ROW
        EXECUTE FUNCTION normalize_model_500_from_planned_risk()
        """
    )

    # Re-run every existing outcome through the normalization trigger. Broker deals stay
    # immutable and untouched; only the derived $500 reporting model is recalculated.
    op.execute(
        "UPDATE performance_trade_outcomes SET derived_at = derived_at"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_normalize_model_500_from_planned_risk "
        "ON performance_trade_outcomes"
    )
    op.execute("DROP FUNCTION IF EXISTS normalize_model_500_from_planned_risk()")
