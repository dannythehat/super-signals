"""Move scalpers and TRADE GLOBAL to shadow-only provider state.

Owner authority, 23 Sep 2026:
* all scalper-style providers are research/shadow only;
* TRADE GLOBAL is removed from broker execution;
* existing broker positions are not force-closed by this migration.

Revision ID: 0114_shadow_scalpers
Revises: 0113_aidy_reasoning_indexes
Create Date: 2026-09-23
"""

from __future__ import annotations

from alembic import op


revision = "0114_shadow_scalpers"
down_revision = "0113_aidy_reasoning_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE sources AS s
        SET status='shadow'
        WHERE s.status IN ('testing','live')
          AND (
            lower(COALESCE(s.source_alias,s.chat_title,''))='trade global'
            OR EXISTS (
                SELECT 1
                FROM provider_research_profiles pr
                WHERE pr.source_id=s.id
                  AND (
                    lower(COALESCE(pr.style,''))='scalper'
                    OR lower(COALESCE(
                        pr.profile_metadata->'adaptive_v1'->'language'->>'cadence_bucket',
                        ''
                    ))='scalper'
                  )
            )
          )
        """
    )


def downgrade() -> None:
    # Deliberately do not auto-promote providers if a rollback is run. Restoring broker
    # execution requires an explicit owner decision, not a schema downgrade side effect.
    pass
