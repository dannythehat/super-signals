"""Protect Provider Lab learned metadata namespaces from destructive writers.

Revision ID: 0065_provider_metadata_guard
Revises: 0064_provider_context_terminal
Create Date: 2026-09-08

Provider discovery owns its sampled metadata, while the adaptive grammar and Provider
Footprint services own their namespaced payloads.  A discovery refresh must never erase
those independent learned namespaces merely because its UPSERT supplies a smaller JSON
object.  This database guard preserves only the reserved learned namespaces when an
UPDATE omits them; explicit replacements supplied by their owning writer still win.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0065_provider_metadata_guard"
down_revision: str | None = "0064_provider_context_terminal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION preserve_provider_learned_metadata_namespaces()
        RETURNS trigger AS $$
        BEGIN
            NEW.profile_metadata := COALESCE(NEW.profile_metadata, '{}'::jsonb);

            IF COALESCE(OLD.profile_metadata, '{}'::jsonb) ? 'adaptive_v1'
               AND NOT NEW.profile_metadata ? 'adaptive_v1' THEN
                NEW.profile_metadata := NEW.profile_metadata || jsonb_build_object(
                    'adaptive_v1', OLD.profile_metadata->'adaptive_v1'
                );
            END IF;

            IF COALESCE(OLD.profile_metadata, '{}'::jsonb) ? 'footprint_v1'
               AND NOT NEW.profile_metadata ? 'footprint_v1' THEN
                NEW.profile_metadata := NEW.profile_metadata || jsonb_build_object(
                    'footprint_v1', OLD.profile_metadata->'footprint_v1'
                );
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_preserve_provider_learned_metadata
        BEFORE UPDATE OF profile_metadata ON provider_research_profiles
        FOR EACH ROW EXECUTE FUNCTION preserve_provider_learned_metadata_namespaces()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_preserve_provider_learned_metadata ON provider_research_profiles"
    )
    op.execute("DROP FUNCTION IF EXISTS preserve_provider_learned_metadata_namespaces()")
