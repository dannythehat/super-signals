"""Add non-blocking lookup indexes for AIDY reasoning.

These indexes only accelerate read-only research queries. They do not alter provider
intake, broker execution, trade management, Telegram publication, or any live-money
authority.

Revision ID: 0113_aidy_reasoning_indexes
Revises: 0112_tig_demo_on
"""

from __future__ import annotations

from alembic import op

revision = "0113_aidy_reasoning_indexes"
down_revision = "0112_tig_demo_on"
branch_labels = None
depends_on = None


def upgrade() -> None:
    context = op.get_context()
    with context.autocommit_block():
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_audit_execution_entry_signal_time
            ON audit_events (entity_id, created_at)
            WHERE entity_type='signal'
              AND event_type='mt5.day26_execution_success'
              AND payload ? 'execution_entry'
            """
        )
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_messages_source_posted
            ON messages (source_id, posted_at DESC)
            """
        )
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_provider_intelligence_evidence_time
            ON provider_intelligence_snapshots
               (source_id, evidence_as_of_utc DESC, created_at DESC)
            """
        )
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_provider_context_message_created
            ON provider_signal_context_attachments (message_id, created_at DESC)
            """
        )


def downgrade() -> None:
    context = op.get_context()
    with context.autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_provider_context_message_created")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_provider_intelligence_evidence_time")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_messages_source_posted")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_audit_execution_entry_signal_time")
