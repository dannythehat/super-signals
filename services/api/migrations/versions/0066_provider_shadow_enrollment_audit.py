"""Add a durable completeness ledger for Provider Lab signal enrollment.

Revision ID: 0066_shadow_enrollment_audit
Revises: 0065_provider_metadata_guard
Create Date: 2026-09-08

Every accepted shadow-provider signal must be either represented by at least one
shadow_trades row or carry an explicit research-only exclusion reason.  This table is
an audit/completeness boundary only; it grants no broker or live-money authority.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0066_shadow_enrollment_audit"
down_revision: str | None = "0065_provider_metadata_guard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE provider_shadow_enrollment_audit (
            signal_id uuid PRIMARY KEY REFERENCES signals(id) ON DELETE CASCADE,
            source_id uuid NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            message_id uuid NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            status varchar(16) NOT NULL CHECK (status IN ('pending','enrolled','excluded')),
            reason varchar(120) NOT NULL,
            provider_signal_posted_at timestamptz NOT NULL,
            enrollment_observed_at timestamptz,
            quote_mode varchar(48),
            entry_delay_ms bigint,
            research_only boolean NOT NULL DEFAULT true CHECK (research_only),
            live_money_execution_allowed boolean NOT NULL DEFAULT false
                CHECK (NOT live_money_execution_allowed),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_provider_shadow_enrollment_source_status "
        "ON provider_shadow_enrollment_audit(source_id,status,provider_signal_posted_at)"
    )

    # Existing benchmark rows are complete. Every accepted shadow signal without a
    # benchmark row starts PENDING so the runtime can make the evidence-dependent choice:
    # repair complete geometry, enroll a genuinely fresh bare NOW profile from a fresh
    # quote, or persist an explicit exclusion. This avoids a deployment-time race.
    op.execute(
        """
        INSERT INTO provider_shadow_enrollment_audit(
            signal_id,source_id,message_id,status,reason,provider_signal_posted_at,
            enrollment_observed_at,quote_mode,entry_delay_ms
        )
        SELECT
            s.id,s.source_id,s.source_message_id,
            CASE WHEN st.signal_id IS NOT NULL THEN 'enrolled' ELSE 'pending' END,
            CASE
                WHEN st.signal_id IS NOT NULL THEN 'existing_shadow_trade'
                WHEN s.entry_low IS NOT NULL AND s.entry_high IS NOT NULL
                     AND s.stop_loss IS NOT NULL
                     AND jsonb_array_length(s.take_profits) > 0 THEN 'structured_repair_pending'
                ELSE 'evidence_review_pending'
            END,
            s.source_posted_at,
            CASE WHEN st.signal_id IS NOT NULL THEN st.created_at ELSE NULL END,
            CASE WHEN st.signal_id IS NOT NULL THEN st.quote_mode ELSE NULL END,
            CASE WHEN st.signal_id IS NOT NULL THEN st.entry_delay_ms ELSE NULL END
        FROM signals s
        JOIN sources src ON src.id=s.source_id
        LEFT JOIN LATERAL (
            SELECT signal_id,created_at,quote_mode,entry_delay_ms
            FROM shadow_trades
            WHERE signal_id=s.id
            ORDER BY created_at ASC
            LIMIT 1
        ) st ON true
        WHERE src.status='shadow' AND s.parser_status='accepted'
        ON CONFLICT (signal_id) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS provider_shadow_enrollment_audit")
