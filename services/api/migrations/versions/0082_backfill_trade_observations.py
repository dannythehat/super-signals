"""Backfill trade observations from decisions already stored.

The interpreter has been reading these messages correctly all along and recording what
it understood in ``ai_message_decisions``. Only the trades that were also executable
went on to become Signals, so everything else was understood, stored, and then never
looked at again.

Those stored payloads are complete -- side, symbol, entry boundaries, stop loss,
targets and management fields are all present on skipped rows -- so five weeks of
provider history can be recovered without asking the model anything a second time. On
production 2026-09-14 that is roughly 15,000 observations across 33 groups, including
2,206 understood new trades and 6,637 understood management updates that had no
research trace at all, and whole providers such as The Gold Club and GOLD VIP whose
every trade was invisible.

Values are copied, never invented. Anything that is not a well-formed number is left
null rather than guessed at, and the executable flag is derived from the action that
was actually taken at the time.

Revision ID: 0082_backfill_trade_observations
Revises: 0081_provider_trade_observations
Create Date: 2026-09-14
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0082_backfill_trade_observations"
down_revision: str | None = "0081_provider_trade_observations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Only a literal decimal is a price. Anything else stays null instead of being guessed.
_NUMERIC = r"^-?[0-9]+(\.[0-9]+)?$"


def _number(field: str) -> str:
    return (
        f"CASE WHEN d.extracted->>'{field}' ~ '{_NUMERIC}' "
        f"THEN (d.extracted->>'{field}')::numeric END"
    )


def upgrade() -> None:
    op.execute(
        f"""
        INSERT INTO provider_trade_observations (
            id,message_id,source_id,revision_index,observed_at,decision,action,
            executable,outcome_reason,symbol,side,order_type,entry_low,entry_high,
            stop_loss,take_profits,tp_open,update_type,update_target,update_value,
            confidence,model,decision_source,raw_text_sha256
        )
        SELECT
            gen_random_uuid(),
            d.message_id,
            m.source_id,
            d.revision_index,
            COALESCE(m.posted_at,m.created_at,d.created_at),
            d.decision,
            d.action,
            (d.decision='new_trade' AND d.action='execute')
                OR (d.decision='trade_update' AND d.action='apply_update'),
            LEFT(COALESCE(NULLIF(d.reason,''),'unspecified'),200),
            LEFT(NULLIF(d.extracted->>'symbol',''),24),
            CASE WHEN upper(d.extracted->>'side') IN ('BUY','SELL')
                 THEN upper(d.extracted->>'side') END,
            LEFT(NULLIF(d.extracted->>'order_type',''),24),
            {_number('entry_low')},
            {_number('entry_high')},
            {_number('stop_loss')},
            CASE WHEN jsonb_typeof(d.extracted->'take_profits')='array'
                 THEN d.extracted->'take_profits' ELSE '[]'::jsonb END,
            COALESCE(d.extracted->>'tp_open' = 'true',false),
            LEFT(NULLIF(d.extracted->>'update_type',''),32),
            LEFT(NULLIF(d.extracted->>'update_target',''),32),
            {_number('update_value')},
            CASE WHEN d.confidence BETWEEN 0 AND 1 THEN d.confidence END,
            LEFT(d.model,64),
            LEFT(COALESCE(NULLIF(d.decision_source,''),'unknown'),32),
            COALESCE(NULLIF(d.raw_text_sha256,''),repeat('0',64))
        FROM ai_message_decisions d
        JOIN messages m ON m.id = d.message_id
        WHERE d.decision IN ('new_trade','trade_update','preparation')
          AND d.action IN ('execute','skip','ignore','apply_update')
        ON CONFLICT (message_id,revision_index) DO NOTHING
        """
    )


def downgrade() -> None:
    # Observations are append-only, so the trigger has to stand aside for the one
    # statement that removes exactly what this migration inserted.
    op.execute(
        "ALTER TABLE provider_trade_observations "
        "DISABLE TRIGGER trg_provider_trade_observations_append_only"
    )
    op.execute(
        """
        DELETE FROM provider_trade_observations o
        USING ai_message_decisions d
        WHERE o.message_id = d.message_id
          AND o.revision_index = d.revision_index
        """
    )
    op.execute(
        "ALTER TABLE provider_trade_observations "
        "ENABLE TRIGGER trg_provider_trade_observations_append_only"
    )
