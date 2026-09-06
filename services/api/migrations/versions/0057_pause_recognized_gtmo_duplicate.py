"""Pause the confirmed duplicate Gold Trader Mo research source.

Revision ID: 0057_pause_gtmo_duplicate
Revises: 0056_provider_profile_versions
Create Date: 2026-09-06

Production evidence on 2026-09-06 showed Gold Trader Mo and GTMO VIP share
341 exact normalized messages, equal to 75.6% of the smaller source corpus.
That exceeds the Provider Lab's existing 0.60 duplicate threshold. GTMO VIP is
the existing TESTING source, so it remains canonical; the shadow duplicate is
paused, not deleted, so its evidence remains auditable and removable later.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0057_pause_gtmo_duplicate"
down_revision: str | None = "0056_provider_profile_versions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DUPLICATE_SOURCE_ID = "bd717166-4c26-4cff-804f-31e8f73d5316"
_CANONICAL_SOURCE_ID = "e772fbc3-cfa7-4ada-a377-e6948d4c5465"


def upgrade() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM sources
                WHERE id = '{_DUPLICATE_SOURCE_ID}'::uuid
                  AND chat_id = -1001519922009
                  AND source_alias = 'Gold Trader Mo🤴🏽'
            ) THEN
                RAISE EXCEPTION 'expected Gold Trader Mo duplicate source identity not found';
            END IF;

            IF NOT EXISTS (
                SELECT 1
                FROM sources
                WHERE id = '{_CANONICAL_SOURCE_ID}'::uuid
                  AND chat_id = -1001640332422
                  AND source_alias = 'GTMO VIP 🤴🏽'
                  AND status IN ('testing', 'live')
            ) THEN
                RAISE EXCEPTION 'expected GTMO VIP canonical source is not active';
            END IF;

            UPDATE provider_research_profiles
            SET research_state = 'duplicate_review',
                duplicate_of_source_id = '{_CANONICAL_SOURCE_ID}'::uuid,
                duplicate_score = 0.75600,
                profile_metadata = COALESCE(profile_metadata, '{{}}'::jsonb)
                    || jsonb_build_object(
                        'duplicate_quarantine_v1',
                        jsonb_build_object(
                            'recognized_at', now(),
                            'basis', 'historical_exact_normalized_message_overlap',
                            'overlap_messages', 341,
                            'smaller_source_messages', 451,
                            'overlap_ratio', 0.756,
                            'threshold', 0.60,
                            'canonical_source_id', '{_CANONICAL_SOURCE_ID}'
                        )
                    ),
                updated_at = now()
            WHERE source_id = '{_DUPLICATE_SOURCE_ID}'::uuid;

            IF NOT FOUND THEN
                RAISE EXCEPTION 'expected Gold Trader Mo provider profile not found';
            END IF;

            UPDATE sources
            SET status = 'paused', updated_at = now()
            WHERE id = '{_DUPLICATE_SOURCE_ID}'::uuid
              AND status <> 'revoked';
        END;
        $$
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        UPDATE sources
        SET status = 'shadow', updated_at = now()
        WHERE id = '{_DUPLICATE_SOURCE_ID}'::uuid
          AND status = 'paused'
        """
    )
    op.execute(
        f"""
        UPDATE provider_research_profiles
        SET research_state = 'learning',
            duplicate_of_source_id = NULL,
            duplicate_score = NULL,
            profile_metadata = COALESCE(profile_metadata, '{{}}'::jsonb)
                - 'duplicate_quarantine_v1',
            updated_at = now()
        WHERE source_id = '{_DUPLICATE_SOURCE_ID}'::uuid
          AND duplicate_of_source_id = '{_CANONICAL_SOURCE_ID}'::uuid
        """
    )
