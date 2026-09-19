"""Version AIDY historical replay cases/decisions for iterative frozen exams.

Revision ID: 0107_aidy_hist_replay_v2
Revises: 0106_aidy_hist_replay
Create Date: 2026-09-19
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0107_aidy_hist_replay_v2"
down_revision: str | None = "0106_aidy_hist_replay"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE aidy_historical_replay_cases
        DROP CONSTRAINT IF EXISTS aidy_historical_replay_cases_source_decision_id_key
        """
    )
    op.execute(
        """
        ALTER TABLE aidy_historical_replay_cases
        ADD CONSTRAINT uq_aidy_hist_case_source_contract
        UNIQUE (source_decision_id,input_contract_version)
        """
    )
    op.execute(
        """
        ALTER TABLE aidy_historical_replay_decisions
        DROP CONSTRAINT IF EXISTS aidy_historical_replay_decisions_case_id_key
        """
    )
    op.execute(
        """
        ALTER TABLE aidy_historical_replay_decisions
        ADD CONSTRAINT uq_aidy_hist_decision_case_replay
        UNIQUE (case_id,replay_version)
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE aidy_historical_replay_decisions
        DROP CONSTRAINT IF EXISTS uq_aidy_hist_decision_case_replay
        """
    )
    op.execute(
        """
        ALTER TABLE aidy_historical_replay_decisions
        ADD CONSTRAINT aidy_historical_replay_decisions_case_id_key UNIQUE (case_id)
        """
    )
    op.execute(
        """
        ALTER TABLE aidy_historical_replay_cases
        DROP CONSTRAINT IF EXISTS uq_aidy_hist_case_source_contract
        """
    )
    op.execute(
        """
        ALTER TABLE aidy_historical_replay_cases
        ADD CONSTRAINT aidy_historical_replay_cases_source_decision_id_key
        UNIQUE (source_decision_id)
        """
    )
