"""Persist AIDY shadow reviews of messages rejected by the upstream interpreter.

These reviews are research-only and cannot place or manage a trade. They give AIDY a
separate learning surface for parser misses such as missing side/SL/instrument and
unsupported management language, while preserving the original observation unchanged.

Revision ID: 0099_aidy_message_reviews
Revises: 0098_aidy_final_shadow_gate
Create Date: 2026-09-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0099_aidy_message_reviews"
down_revision: str | None = "0098_aidy_final_shadow_gate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE aidy_message_reviews (
          id uuid PRIMARY KEY,
          observation_id uuid NOT NULL UNIQUE
            REFERENCES provider_trade_observations(id) ON DELETE CASCADE,
          source_id uuid NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
          observed_at timestamptz NOT NULL,
          original_decision varchar(64) NOT NULL,
          original_action varchar(64) NOT NULL,
          original_outcome_reason text,
          review_class varchar(32) NOT NULL
            CHECK (review_class IN (
              'likely_trade','likely_management','correct_skip','uncertain'
            )),
          suggested_action varchar(32) NOT NULL
            CHECK (suggested_action IN (
              'execute_candidate','management_candidate','ignore','needs_rule_review'
            )),
          confidence numeric(6,5) NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
          missing_fields jsonb NOT NULL DEFAULT '[]'::jsonb,
          rationale text NOT NULL,
          suggested_parser_rule text,
          provider_profile_version_no integer,
          model_version varchar(128) NOT NULL,
          prompt_version varchar(128) NOT NULL,
          model_name varchar(128) NOT NULL,
          response_id varchar(255),
          input_tokens integer NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
          output_tokens integer NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
          estimated_cost_usd numeric(12,8) NOT NULL DEFAULT 0 CHECK (estimated_cost_usd >= 0),
          latency_ms integer NOT NULL DEFAULT 0 CHECK (latency_ms >= 0),
          research_only boolean NOT NULL DEFAULT true CHECK (research_only),
          live_execution_affected boolean NOT NULL DEFAULT false CHECK (NOT live_execution_affected),
          live_money_execution_allowed boolean NOT NULL DEFAULT false
            CHECK (NOT live_money_execution_allowed),
          created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_aidy_message_reviews_source_created
          ON aidy_message_reviews(source_id,created_at DESC)
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION aidy_message_reviews_append_only()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          RAISE EXCEPTION 'aidy_message_reviews is append-only';
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_aidy_message_reviews_append_only
        BEFORE UPDATE OR DELETE ON aidy_message_reviews
        FOR EACH ROW EXECUTE FUNCTION aidy_message_reviews_append_only()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE VIEW aidy_message_review_summary AS
        SELECT
          s.id AS source_id,
          COALESCE(NULLIF(s.source_alias,''),s.chat_title) AS provider,
          count(*) AS reviewed,
          count(*) FILTER (WHERE r.review_class='likely_trade') AS likely_trade_misses,
          count(*) FILTER (WHERE r.review_class='likely_management') AS likely_management_misses,
          count(*) FILTER (WHERE r.review_class='correct_skip') AS confirmed_skips,
          count(*) FILTER (WHERE r.review_class='uncertain') AS uncertain,
          round(avg(r.confidence),3) AS avg_confidence,
          max(r.created_at) AS latest_review
        FROM aidy_message_reviews r
        JOIN sources s ON s.id=r.source_id
        GROUP BY s.id,COALESCE(NULLIF(s.source_alias,''),s.chat_title)
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS aidy_message_review_summary")
    op.execute("DROP TRIGGER IF EXISTS trg_aidy_message_reviews_append_only ON aidy_message_reviews")
    op.execute("DROP FUNCTION IF EXISTS aidy_message_reviews_append_only()")
    op.execute("DROP TABLE IF EXISTS aidy_message_reviews")
