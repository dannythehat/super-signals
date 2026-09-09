"""Require provider-specific profile evidence before Provider Lab scoring.

Revision ID: 0070_provider_profile_gate0
Revises: 0069_provider_day20_mgmt
Create Date: 2026-09-09

Provider messages may always be ingested and used to improve that provider's own
profile.  A shadow outcome can become score-eligible only when the immutable
profile version attached at signal time had already learned enough provider-specific
signal grammar, timing, message behaviour and trade geometry.  This keeps the gate
forward-only and prevents later learning from legitimising an older trade.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0070_provider_profile_gate0"
down_revision: str | None = "0069_provider_day20_mgmt"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE VIEW provider_profile_gate_status AS
        WITH profile AS (
            SELECT
                s.id AS source_id,
                COALESCE(NULLIF(s.source_alias,''),NULLIF(s.chat_title,''),'unknown') AS provider_name,
                s.status AS source_status,
                p.interpretation_readiness,
                p.updated_at AS profile_updated_at,
                p.profile_metadata,
                p.profile_metadata->'adaptive_v1'->'language' AS language,
                p.profile_metadata->'footprint_v1' AS footprint
            FROM sources s
            LEFT JOIN provider_research_profiles p ON p.source_id=s.id
            WHERE s.status IN ('testing','shadow','live')
        ), evidence AS (
            SELECT
                *,
                GREATEST(
                    COALESCE((language->>'accepted_signal_count')::int,0),
                    COALESCE((footprint->'trade_geometry'->>'accepted_signals')::int,0)
                ) AS accepted_signals,
                COALESCE((footprint->'timing_fingerprint'->>'active_days')::int,0) AS active_days,
                COALESCE((footprint->'message_behaviour'->>'observed_messages')::int,0) AS observed_messages,
                COALESCE(footprint->'interpretation_context'->>'entry_style','unknown') AS entry_style,
                COALESCE(footprint->'interpretation_context'->>'order_style','unknown') AS order_style,
                COALESCE(footprint->'interpretation_context'->>'direction_style','unknown') AS direction_style,
                COALESCE(footprint->'interpretation_context'->>'message_format','unknown') AS message_format,
                COALESCE(footprint->'interpretation_context'->>'message_sequence','unknown') AS message_sequence,
                COALESCE(footprint->'interpretation_context'->>'dominant_session_utc','unknown') AS dominant_session_utc,
                COALESCE(footprint->'interpretation_context'->>'management_style','unknown') AS management_style,
                footprint->'trade_geometry'->'median_stop_distance' AS median_stop_distance,
                footprint->'trade_geometry'->'median_tp_count' AS median_tp_count,
                CASE
                    WHEN jsonb_typeof(footprint->'interpretation_context'->'provider_vocabulary')='array'
                    THEN jsonb_array_length(footprint->'interpretation_context'->'provider_vocabulary')
                    ELSE 0
                END AS provider_vocabulary_count,
                CASE
                    WHEN jsonb_typeof(language->'grammar_examples_masked'->'new_trade')='array'
                    THEN jsonb_array_length(language->'grammar_examples_masked'->'new_trade')
                    ELSE 0
                END AS new_trade_example_count
            FROM profile
        ), gated AS (
            SELECT
                *,
                array_remove(ARRAY[
                    CASE WHEN NOT COALESCE(profile_metadata ? 'adaptive_v1',false) THEN 'adaptive_profile_missing' END,
                    CASE WHEN NOT COALESCE(profile_metadata ? 'footprint_v1',false) THEN 'provider_footprint_missing' END,
                    CASE WHEN accepted_signals < 3 THEN 'insufficient_understood_signals' END,
                    CASE WHEN active_days < 3 THEN 'insufficient_active_days' END,
                    CASE WHEN observed_messages < 30 THEN 'insufficient_message_history' END,
                    CASE WHEN entry_style='unknown' THEN 'entry_style_unknown' END,
                    CASE WHEN order_style='unknown' THEN 'order_style_unknown' END,
                    CASE WHEN direction_style='unknown' THEN 'direction_style_unknown' END,
                    CASE WHEN message_format='unknown' THEN 'message_format_unknown' END,
                    CASE WHEN message_sequence='unknown' THEN 'message_sequence_unknown' END,
                    CASE WHEN dominant_session_utc='unknown' THEN 'dominant_session_utc_unknown' END,
                    CASE WHEN management_style='unknown' THEN 'management_style_unknown' END,
                    CASE WHEN median_stop_distance IS NULL THEN 'stop_geometry_unknown' END,
                    CASE WHEN median_tp_count IS NULL THEN 'target_geometry_unknown' END,
                    CASE WHEN provider_vocabulary_count < 3 THEN 'provider_vocabulary_insufficient' END,
                    CASE WHEN new_trade_example_count < 1 THEN 'new_trade_grammar_examples_missing' END
                ],NULL) AS blockers
            FROM evidence
        )
        SELECT
            source_id,
            provider_name,
            source_status,
            'provider-profile-gate-v1'::varchar(64) AS gate_version,
            cardinality(blockers)=0 AS qualified,
            CASE WHEN cardinality(blockers)=0 THEN 'qualified' ELSE 'profiling' END::varchar(24) AS gate_state,
            blockers,
            accepted_signals,
            active_days,
            observed_messages,
            entry_style,
            order_style,
            direction_style,
            message_format,
            message_sequence,
            dominant_session_utc,
            management_style,
            provider_vocabulary_count,
            new_trade_example_count,
            median_stop_distance,
            median_tp_count,
            interpretation_readiness,
            profile_updated_at
        FROM gated
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION provider_profile_gate0_snapshot_qualified(p_snapshot jsonb)
        RETURNS boolean
        LANGUAGE sql
        IMMUTABLE
        AS $$
            WITH v AS (
                SELECT
                    p_snapshot->'profile_metadata' AS metadata,
                    p_snapshot->'profile_metadata'->'adaptive_v1'->'language' AS language,
                    p_snapshot->'profile_metadata'->'footprint_v1' AS footprint
            ), e AS (
                SELECT
                    metadata,
                    language,
                    footprint,
                    GREATEST(
                        COALESCE((language->>'accepted_signal_count')::int,0),
                        COALESCE((footprint->'trade_geometry'->>'accepted_signals')::int,0)
                    ) AS accepted_signals,
                    COALESCE((footprint->'timing_fingerprint'->>'active_days')::int,0) AS active_days,
                    COALESCE((footprint->'message_behaviour'->>'observed_messages')::int,0) AS observed_messages,
                    COALESCE(footprint->'interpretation_context'->>'entry_style','unknown') AS entry_style,
                    COALESCE(footprint->'interpretation_context'->>'order_style','unknown') AS order_style,
                    COALESCE(footprint->'interpretation_context'->>'direction_style','unknown') AS direction_style,
                    COALESCE(footprint->'interpretation_context'->>'message_format','unknown') AS message_format,
                    COALESCE(footprint->'interpretation_context'->>'message_sequence','unknown') AS message_sequence,
                    COALESCE(footprint->'interpretation_context'->>'dominant_session_utc','unknown') AS dominant_session_utc,
                    COALESCE(footprint->'interpretation_context'->>'management_style','unknown') AS management_style,
                    footprint->'trade_geometry'->'median_stop_distance' AS median_stop_distance,
                    footprint->'trade_geometry'->'median_tp_count' AS median_tp_count,
                    CASE
                        WHEN jsonb_typeof(footprint->'interpretation_context'->'provider_vocabulary')='array'
                        THEN jsonb_array_length(footprint->'interpretation_context'->'provider_vocabulary')
                        ELSE 0
                    END AS provider_vocabulary_count,
                    CASE
                        WHEN jsonb_typeof(language->'grammar_examples_masked'->'new_trade')='array'
                        THEN jsonb_array_length(language->'grammar_examples_masked'->'new_trade')
                        ELSE 0
                    END AS new_trade_example_count
                FROM v
            )
            SELECT
                COALESCE(metadata ? 'adaptive_v1',false)
                AND COALESCE(metadata ? 'footprint_v1',false)
                AND accepted_signals >= 3
                AND active_days >= 3
                AND observed_messages >= 30
                AND entry_style <> 'unknown'
                AND order_style <> 'unknown'
                AND direction_style <> 'unknown'
                AND message_format <> 'unknown'
                AND message_sequence <> 'unknown'
                AND dominant_session_utc <> 'unknown'
                AND management_style <> 'unknown'
                AND median_stop_distance IS NOT NULL
                AND median_tp_count IS NOT NULL
                AND provider_vocabulary_count >= 3
                AND new_trade_example_count >= 1
            FROM e
        $$
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_shadow_provider_profile_gate0()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_snapshot jsonb;
        BEGIN
            IF NOT COALESCE(NEW.score_eligible,false) THEN
                RETURN NEW;
            END IF;

            SELECT profile_snapshot
            INTO v_snapshot
            FROM provider_research_profile_versions
            WHERE id=NEW.provider_profile_version_id
              AND source_id=NEW.source_id
              AND effective_at<=NEW.signal_posted_at
            LIMIT 1;

            IF v_snapshot IS NULL OR NOT provider_profile_gate0_snapshot_qualified(v_snapshot) THEN
                NEW.score_eligible := false;
                NEW.score_exclusion_reason := 'provider_profile_gate_incomplete';
                NEW.aidy_score_blocked := true;
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_shadow_provider_profile_gate0
        BEFORE INSERT OR UPDATE OF score_eligible ON shadow_trades
        FOR EACH ROW EXECUTE FUNCTION enforce_shadow_provider_profile_gate0()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_shadow_provider_profile_gate0 ON shadow_trades")
    op.execute("DROP FUNCTION IF EXISTS enforce_shadow_provider_profile_gate0()")
    op.execute("DROP FUNCTION IF EXISTS provider_profile_gate0_snapshot_qualified(jsonb)")
    op.execute("DROP VIEW IF EXISTS provider_profile_gate_status")
