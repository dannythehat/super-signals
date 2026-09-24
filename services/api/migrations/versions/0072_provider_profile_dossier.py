"""Expose one auditable current provider dossier per monitored signal source.

Revision ID: 0072_provider_profile_dossier
Revises: 0071_provider_gate0_existing
Create Date: 2026-09-09
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0072_provider_profile_dossier"
down_revision: str | None = "0071_provider_gate0_existing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE VIEW provider_profile_dossier AS
        SELECT
            g.source_id,
            g.provider_name,
            g.source_status,
            g.gate_version,
            g.gate_state,
            g.qualified,
            g.blockers,
            p.research_state,
            p.style,
            p.interpretation_readiness,
            p.signal_likelihood,
            g.observed_messages,
            g.active_days,
            p.profile_metadata->'footprint_v1'->'message_behaviour'->>'messages_per_active_day'
                AS messages_per_active_day,
            p.profile_metadata->'footprint_v1'->'message_behaviour'->>'edit_rate' AS edit_rate,
            p.profile_metadata->'footprint_v1'->'message_behaviour'->>'reply_rate' AS reply_rate,
            p.profile_metadata->'footprint_v1'->'message_behaviour'->>'delete_rate' AS delete_rate,
            p.profile_metadata->'footprint_v1'->'message_behaviour'->>'median_first_edit_delay_minutes'
                AS median_first_edit_delay_minutes,
            g.dominant_session_utc,
            p.profile_metadata->'footprint_v1'->'timing_fingerprint'->'session_mix' AS session_mix,
            g.accepted_signals,
            g.entry_style,
            g.order_style,
            g.direction_style,
            p.profile_metadata->'footprint_v1'->'trade_geometry'->'entry_style_mix' AS entry_style_mix,
            p.profile_metadata->'footprint_v1'->'trade_geometry'->'order_type_mix' AS order_type_mix,
            p.profile_metadata->'footprint_v1'->'trade_geometry'->'side_mix' AS side_mix,
            p.profile_metadata->'footprint_v1'->'trade_geometry'->'median_entry_zone_width'
                AS median_entry_zone_width,
            g.median_stop_distance,
            g.median_tp_count,
            p.profile_metadata->'footprint_v1'->'trade_geometry'->>'runner_rate' AS runner_rate,
            p.profile_metadata->'footprint_v1'->'trade_geometry'->>'signal_from_revision_rate'
                AS signal_from_revision_rate,
            g.management_style,
            p.profile_metadata->'footprint_v1'->'management_fingerprint'->>'provider_update_events'
                AS provider_update_events,
            p.profile_metadata->'footprint_v1'->'management_fingerprint'->>'managed_signal_count'
                AS managed_signal_count,
            p.profile_metadata->'footprint_v1'->'management_fingerprint'->>'events_per_signal'
                AS management_events_per_signal,
            p.profile_metadata->'footprint_v1'->'management_fingerprint'->'event_mix'
                AS management_event_mix,
            p.profile_metadata->'footprint_v1'->'management_fingerprint'->>'median_first_management_delay_minutes'
                AS median_first_management_delay_minutes,
            g.message_format,
            g.message_sequence,
            p.profile_metadata->'footprint_v1'->'interpretation_context'->'provider_vocabulary'
                AS provider_vocabulary,
            p.profile_metadata->'footprint_v1'->'interpretation_context'->>'runner_usage'
                AS runner_usage,
            p.profile_metadata->'footprint_v1'->'interpretation_context'->>'edit_behaviour'
                AS edit_behaviour,
            p.profile_metadata->'footprint_v1'->'interpretation_context'->>'reply_behaviour'
                AS reply_behaviour,
            p.profile_metadata->'footprint_v1'->'interpretation_context'->>'drift_status'
                AS drift_status,
            p.profile_metadata->'adaptive_v1'->'language'->>'cadence_bucket' AS cadence_bucket,
            p.profile_metadata->'adaptive_v1'->'language'->>'entry_bucket' AS adaptive_entry_bucket,
            p.profile_metadata->'adaptive_v1'->'language'->>'order_bucket' AS adaptive_order_bucket,
            p.profile_metadata->'adaptive_v1'->'language'->>'sequence_bucket' AS adaptive_sequence_bucket,
            p.profile_metadata->'adaptive_v1'->'language'->>'management_bucket' AS adaptive_management_bucket,
            p.profile_metadata->'adaptive_v1'->'language'->>'signals_per_active_day'
                AS adaptive_signals_per_active_day,
            p.profile_metadata->'adaptive_v1'->'language'->'traits' AS adaptive_language_traits,
            p.profile_metadata->'adaptive_v1'->'language'->'grammar_examples_masked'
                AS masked_grammar_examples,
            p.profile_metadata->'footprint_v1'->'identity_fingerprint'->>'behaviour_signature_sha256'
                AS behaviour_signature_sha256,
            p.profile_metadata->'footprint_v1'->>'evidence_as_of_utc' AS evidence_as_of_utc,
            g.profile_updated_at
        FROM provider_profile_gate_status g
        JOIN provider_research_profiles p ON p.source_id=g.source_id
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS provider_profile_dossier")
