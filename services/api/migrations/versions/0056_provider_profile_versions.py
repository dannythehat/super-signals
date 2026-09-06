"""Add immutable point-in-time provider identity/profile version history.

Revision ID: 0056_provider_profile_versions
Revises: 0055_aidy_provider_lab_truth
Create Date: 2026-09-06

Day 7 begins the provider-intelligence phase without changing broker execution.
The mutable provider_research_profiles row remains the current-state cache; this
migration adds an append-only history that records only meaningful identity,
style and behavioural-profile changes. Existing rows are bootstrapped at the
migration timestamp and are never backdated as if historical versions existed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0056_provider_profile_versions"
down_revision: str | None = "0055_aidy_provider_lab_truth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_research_profile_versions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(length=40), nullable=False),
        sa.Column("change_source", sa.String(length=40), nullable=False),
        sa.Column(
            "source_identity",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "profile_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("snapshot_fingerprint", sa.String(length=32), nullable=False),
        sa.Column(
            "effective_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "version_no > 0",
            name="ck_provider_profile_version_positive",
        ),
        sa.CheckConstraint(
            "schema_version = 'provider-profile-history-v1'",
            name="ck_provider_profile_history_schema",
        ),
        sa.CheckConstraint(
            "change_source IN ('bootstrap_current_state','profile_insert','profile_update','source_identity_update')",
            name="ck_provider_profile_change_source",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_identity) = 'object' AND jsonb_typeof(profile_snapshot) = 'object'",
            name="ck_provider_profile_version_json_objects",
        ),
        sa.CheckConstraint(
            "length(snapshot_fingerprint) = 32",
            name="ck_provider_profile_snapshot_fingerprint",
        ),
        sa.UniqueConstraint(
            "source_id",
            "version_no",
            name="uq_provider_profile_source_version",
        ),
    )
    op.create_index(
        "ix_provider_profile_versions_source_effective",
        "provider_research_profile_versions",
        ["source_id", "effective_at", "version_no"],
    )

    op.execute(
        """
        CREATE FUNCTION prevent_provider_profile_version_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'provider_research_profile_versions are append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_provider_profile_versions_append_only
        BEFORE UPDATE OR DELETE ON provider_research_profile_versions
        FOR EACH ROW EXECUTE FUNCTION prevent_provider_profile_version_mutation()
        """
    )

    # One serialization lock per provider prevents simultaneous profile/source
    # refreshes from allocating the same version number. The adaptive profile's
    # generated_at_epoch is deliberately excluded from the semantic snapshot so
    # a no-op 15-minute refresh does not manufacture a new history version.
    op.execute(
        """
        CREATE FUNCTION record_provider_research_profile_version(
            p_source_id uuid,
            p_change_source text
        ) RETURNS uuid AS $$
        DECLARE
            v_identity jsonb;
            v_profile jsonb;
            v_fingerprint text;
            v_latest_fingerprint text;
            v_latest_version integer;
            v_version_id uuid;
        BEGIN
            PERFORM pg_advisory_xact_lock(
                hashtext('provider-profile-version:' || p_source_id::text)
            );

            SELECT jsonb_build_object(
                       'source_id', s.id::text,
                       'chat_id', s.chat_id,
                       'chat_title', s.chat_title,
                       'source_alias', s.source_alias,
                       'status', s.status
                   )
            INTO v_identity
            FROM sources s
            WHERE s.id = p_source_id;

            IF v_identity IS NULL THEN
                RETURN NULL;
            END IF;

            SELECT jsonb_build_object(
                       'research_state', p.research_state,
                       'style', p.style,
                       'observed_messages', p.observed_messages,
                       'signal_like_messages', p.signal_like_messages,
                       'structured_signal_messages', p.structured_signal_messages,
                       'management_messages', p.management_messages,
                       'edited_messages', p.edited_messages,
                       'signal_likelihood', p.signal_likelihood,
                       'interpretation_readiness', p.interpretation_readiness,
                       'duplicate_of_source_id', CASE
                           WHEN p.duplicate_of_source_id IS NULL THEN NULL
                           ELSE p.duplicate_of_source_id::text
                       END,
                       'duplicate_score', p.duplicate_score,
                       'profile_metadata',
                           COALESCE(p.profile_metadata, '{}'::jsonb)
                           #- '{adaptive_v1,generated_at_epoch}'
                   )
            INTO v_profile
            FROM provider_research_profiles p
            WHERE p.source_id = p_source_id;

            IF v_profile IS NULL THEN
                RETURN NULL;
            END IF;

            v_fingerprint := md5(v_identity::text || '|' || v_profile::text);

            SELECT snapshot_fingerprint, version_no
            INTO v_latest_fingerprint, v_latest_version
            FROM provider_research_profile_versions
            WHERE source_id = p_source_id
            ORDER BY version_no DESC
            LIMIT 1;

            IF v_latest_fingerprint IS NOT NULL
               AND v_latest_fingerprint = v_fingerprint THEN
                RETURN NULL;
            END IF;

            INSERT INTO provider_research_profile_versions(
                source_id,
                version_no,
                schema_version,
                change_source,
                source_identity,
                profile_snapshot,
                snapshot_fingerprint
            ) VALUES (
                p_source_id,
                COALESCE(v_latest_version, 0) + 1,
                'provider-profile-history-v1',
                p_change_source,
                v_identity,
                v_profile,
                v_fingerprint
            )
            RETURNING id INTO v_version_id;

            RETURN v_version_id;
        END;
        $$ LANGUAGE plpgsql
        """
    )

    op.execute(
        """
        CREATE FUNCTION capture_provider_research_profile_version()
        RETURNS trigger AS $$
        BEGIN
            PERFORM record_provider_research_profile_version(
                NEW.source_id,
                CASE WHEN TG_OP = 'INSERT' THEN 'profile_insert' ELSE 'profile_update' END
            );
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_provider_research_profile_version
        AFTER INSERT OR UPDATE ON provider_research_profiles
        FOR EACH ROW EXECUTE FUNCTION capture_provider_research_profile_version()
        """
    )

    op.execute(
        """
        CREATE FUNCTION capture_provider_source_identity_version()
        RETURNS trigger AS $$
        BEGIN
            PERFORM record_provider_research_profile_version(
                NEW.id,
                'source_identity_update'
            );
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_provider_source_identity_version
        AFTER UPDATE OF chat_id, chat_title, source_alias, status ON sources
        FOR EACH ROW
        WHEN (
            OLD.chat_id IS DISTINCT FROM NEW.chat_id
            OR OLD.chat_title IS DISTINCT FROM NEW.chat_title
            OR OLD.source_alias IS DISTINCT FROM NEW.source_alias
            OR OLD.status IS DISTINCT FROM NEW.status
        )
        EXECUTE FUNCTION capture_provider_source_identity_version()
        """
    )

    # PIT reader: return only the version that was already effective at the
    # requested timestamp. No future profile state can leak backwards.
    op.execute(
        """
        CREATE FUNCTION provider_research_profile_version_as_of(
            p_source_id uuid,
            p_as_of timestamptz
        ) RETURNS SETOF provider_research_profile_versions AS $$
            SELECT v.*
            FROM provider_research_profile_versions v
            WHERE v.source_id = p_source_id
              AND v.effective_at <= p_as_of
            ORDER BY v.effective_at DESC, v.version_no DESC
            LIMIT 1
        $$ LANGUAGE sql STABLE
        """
    )

    # Establish a forward-only history origin. This is a snapshot of what is
    # known at migration time; it intentionally does not fabricate earlier
    # versions from historical Telegram or performance data.
    op.execute(
        """
        DO $$
        DECLARE
            r record;
        BEGIN
            FOR r IN
                SELECT source_id
                FROM provider_research_profiles
                ORDER BY source_id
            LOOP
                PERFORM record_provider_research_profile_version(
                    r.source_id,
                    'bootstrap_current_state'
                );
            END LOOP;
        END;
        $$
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS provider_research_profile_version_as_of(uuid,timestamptz)"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_provider_source_identity_version ON sources"
    )
    op.execute("DROP FUNCTION IF EXISTS capture_provider_source_identity_version()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_provider_research_profile_version ON provider_research_profiles"
    )
    op.execute("DROP FUNCTION IF EXISTS capture_provider_research_profile_version()")
    op.execute("DROP FUNCTION IF EXISTS record_provider_research_profile_version(uuid,text)")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_provider_profile_versions_append_only ON provider_research_profile_versions"
    )
    op.execute("DROP FUNCTION IF EXISTS prevent_provider_profile_version_mutation()")
    op.drop_index(
        "ix_provider_profile_versions_source_effective",
        table_name="provider_research_profile_versions",
    )
    op.drop_table("provider_research_profile_versions")
