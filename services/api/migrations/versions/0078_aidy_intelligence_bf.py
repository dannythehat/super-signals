"""Persist AIDY Provider Intelligence B-F research snapshots.

Revision ID: 0078_aidy_intel_bf
Revises: 0077_enable_scalper_aidy_m1
Create Date: 2026-09-13
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0078_aidy_intel_bf"
down_revision: str | None = "0077_enable_scalper_aidy_m1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE provider_intelligence_snapshots (
            id uuid PRIMARY KEY,
            source_id uuid NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            observed_at timestamptz NOT NULL,
            evidence_as_of_utc timestamptz NOT NULL,
            market_context_json jsonb NOT NULL DEFAULT '{}'::jsonb,
            fingerprint_json jsonb NOT NULL DEFAULT '{}'::jsonb,
            governance_json jsonb NOT NULL DEFAULT '{}'::jsonb,
            adaptation_json jsonb NOT NULL DEFAULT '{}'::jsonb,
            snapshot_payload jsonb NOT NULL,
            snapshot_digest varchar(64) NOT NULL,
            contract_version varchar(64) NOT NULL,
            research_only boolean NOT NULL DEFAULT true CHECK (research_only),
            live_money_execution_allowed boolean NOT NULL DEFAULT false
                CHECK (NOT live_money_execution_allowed),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(source_id,snapshot_digest)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_provider_intelligence_source_time "
        "ON provider_intelligence_snapshots(source_id,observed_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_provider_intelligence_governance "
        "ON provider_intelligence_snapshots((governance_json->>'research_disposition'),observed_at DESC)"
    )

    op.execute(
        """
        CREATE TABLE provider_book_conflict_snapshots (
            id uuid PRIMARY KEY,
            observed_at timestamptz NOT NULL,
            window_start_utc timestamptz NOT NULL,
            window_end_utc timestamptz NOT NULL,
            signal_count integer NOT NULL CHECK (signal_count >= 0),
            provider_count integer NOT NULL CHECK (provider_count >= 0),
            buy_provider_count integer NOT NULL CHECK (buy_provider_count >= 0),
            sell_provider_count integer NOT NULL CHECK (sell_provider_count >= 0),
            conflict_count integer NOT NULL CHECK (conflict_count >= 0),
            net_bias varchar(16) NOT NULL CHECK (net_bias IN ('long','short','flat','mixed')),
            conflicts_json jsonb NOT NULL DEFAULT '[]'::jsonb,
            snapshot_payload jsonb NOT NULL,
            snapshot_digest varchar(64) NOT NULL UNIQUE,
            contract_version varchar(64) NOT NULL,
            research_only boolean NOT NULL DEFAULT true CHECK (research_only),
            live_money_execution_allowed boolean NOT NULL DEFAULT false
                CHECK (NOT live_money_execution_allowed),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_provider_book_conflict_time "
        "ON provider_book_conflict_snapshots(observed_at DESC)"
    )

    op.execute(
        """
        CREATE VIEW provider_intelligence_current AS
        SELECT DISTINCT ON (p.source_id)
               p.id,p.source_id,s.chat_title,s.source_alias,s.status AS source_status,
               p.observed_at,p.evidence_as_of_utc,p.market_context_json,p.fingerprint_json,
               p.governance_json,p.adaptation_json,p.snapshot_payload,p.snapshot_digest,
               p.contract_version,p.research_only,p.live_money_execution_allowed
        FROM provider_intelligence_snapshots p
        JOIN sources s ON s.id=p.source_id
        ORDER BY p.source_id,p.observed_at DESC,p.created_at DESC,p.id DESC
        """
    )
    op.execute(
        """
        CREATE VIEW provider_book_conflict_current AS
        SELECT p.*
        FROM provider_book_conflict_snapshots p
        ORDER BY p.observed_at DESC,p.created_at DESC,p.id DESC
        LIMIT 1
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION provider_intelligence_bf_append_only()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'AIDY Provider Intelligence B-F evidence is append-only';
        END;
        $$
        """
    )
    for table in ("provider_intelligence_snapshots", "provider_book_conflict_snapshots"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION provider_intelligence_bf_append_only()"
        )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS provider_book_conflict_current")
    op.execute("DROP VIEW IF EXISTS provider_intelligence_current")
    op.execute("DROP TABLE IF EXISTS provider_book_conflict_snapshots")
    op.execute("DROP TABLE IF EXISTS provider_intelligence_snapshots")
    op.execute("DROP FUNCTION IF EXISTS provider_intelligence_bf_append_only()")
