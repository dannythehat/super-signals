"""Allow immutable AIDY context attachment before shadow-trade enrollment.

A context packet belongs to the accepted signal and its PIT-clean provider profile. Requiring
a shadow_trades row first delayed or prevented context for signals that AIDY needs to reason
about as the final shadow layer. This migration removes only that sequencing dependency; all
signal/profile temporal constraints and the no-live-money boundary remain unchanged.

Revision ID: 0100_aidy_context_all_signals
Revises: 0099_aidy_message_reviews
Create Date: 2026-09-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0100_aidy_context_all_signals"
down_revision: str | None = "0099_aidy_message_reviews"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_NEW_FUNCTION = """
CREATE OR REPLACE FUNCTION enforce_provider_signal_context_attachment()
RETURNS trigger AS $$
BEGIN
    PERFORM 1
    FROM signals s
    JOIN messages m ON m.id=s.source_message_id
    WHERE s.id=NEW.signal_id
      AND s.source_id=NEW.source_id
      AND m.id=NEW.message_id
      AND m.source_id=NEW.source_id
      AND s.source_posted_at=NEW.signal_posted_at
      AND s.parser_status='accepted'
      AND s.symbol='XAUUSD';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'provider context signal provenance invalid';
    END IF;

    PERFORM 1
    FROM provider_research_profile_versions v
    WHERE v.id=NEW.provider_profile_version_id
      AND v.source_id=NEW.source_id
      AND v.version_no=NEW.provider_profile_version_no
      AND v.effective_at=NEW.provider_profile_effective_at
      AND v.effective_at<=NEW.signal_posted_at;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'provider context profile provenance invalid';
    END IF;

    IF NEW.aidy_requested_as_of_utc<>NEW.signal_posted_at
       OR NEW.aidy_context_as_of_utc>NEW.signal_posted_at
       OR NEW.provider_profile_effective_at>NEW.signal_posted_at THEN
        RAISE EXCEPTION 'provider context point-in-time boundary invalid';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

_OLD_FUNCTION = """
CREATE OR REPLACE FUNCTION enforce_provider_signal_context_attachment()
RETURNS trigger AS $$
BEGIN
    PERFORM 1
    FROM signals s
    JOIN messages m ON m.id=s.source_message_id
    WHERE s.id=NEW.signal_id
      AND m.id=NEW.message_id
      AND m.source_id=NEW.source_id
      AND s.source_posted_at=NEW.signal_posted_at;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'provider context signal provenance invalid';
    END IF;

    PERFORM 1
    FROM provider_research_profile_versions v
    WHERE v.id=NEW.provider_profile_version_id
      AND v.source_id=NEW.source_id
      AND v.version_no=NEW.provider_profile_version_no
      AND v.effective_at=NEW.provider_profile_effective_at
      AND v.effective_at<=NEW.signal_posted_at;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'provider context profile provenance invalid';
    END IF;

    PERFORM 1
    FROM shadow_trades t
    WHERE t.signal_id=NEW.signal_id
      AND t.source_id=NEW.source_id
      AND t.provider_profile_pit_status='resolved'
      AND t.provider_profile_version_id=NEW.provider_profile_version_id
      AND t.provider_profile_version_no=NEW.provider_profile_version_no
      AND t.provider_profile_effective_at=NEW.provider_profile_effective_at;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'provider context requires resolved shadow research anchor';
    END IF;

    IF NEW.aidy_requested_as_of_utc<>NEW.signal_posted_at
       OR NEW.aidy_context_as_of_utc>NEW.signal_posted_at
       OR NEW.provider_profile_effective_at>NEW.signal_posted_at THEN
        RAISE EXCEPTION 'provider context point-in-time boundary invalid';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.execute(_NEW_FUNCTION)


def downgrade() -> None:
    op.execute(_OLD_FUNCTION)
