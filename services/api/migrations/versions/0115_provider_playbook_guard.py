"""Preserve provider management/playbook learned metadata namespaces.

Revision ID: 0115_provider_playbook_guard
Revises: 0114_enable_shadow_providers
Create Date: 2026-09-23
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0115_provider_playbook_guard"
down_revision: str | None = "0114_enable_shadow_providers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION preserve_provider_learned_metadata_namespaces()
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

            IF COALESCE(OLD.profile_metadata, '{}'::jsonb) ? 'management_language_audit_v1'
               AND NOT NEW.profile_metadata ? 'management_language_audit_v1' THEN
                NEW.profile_metadata := NEW.profile_metadata || jsonb_build_object(
                    'management_language_audit_v1',
                    OLD.profile_metadata->'management_language_audit_v1'
                );
            END IF;

            IF COALESCE(OLD.profile_metadata, '{}'::jsonb) ? 'provider_playbook_v1'
               AND NOT NEW.profile_metadata ? 'provider_playbook_v1' THEN
                NEW.profile_metadata := NEW.profile_metadata || jsonb_build_object(
                    'provider_playbook_v1', OLD.profile_metadata->'provider_playbook_v1'
                );
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )


def downgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION preserve_provider_learned_metadata_namespaces()
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
