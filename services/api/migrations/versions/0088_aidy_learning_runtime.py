"""Make Provider Intelligence context recovery and Day 13 refresh repeatable.

Revision ID: 0088_aidy_learning_runtime
Revises: 0087_provider_scoreboard
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0088_aidy_learning_runtime"
down_revision: str | None = "0087_provider_scoreboard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Day 13 used to be unique per code SHA. A long-running deployment therefore
    # returned the first completed run forever even as new forward evidence arrived.
    # Keep the source SHA intact and add a UTC evidence day as the bounded refresh key.
    op.add_column(
        "provider_conditional_runs",
        sa.Column(
            "run_day",
            sa.Date(),
            nullable=True,
            server_default=sa.text("((now() AT TIME ZONE 'UTC')::date)"),
        ),
    )
    op.execute(
        "UPDATE provider_conditional_runs "
        "SET run_day=(evidence_cutoff AT TIME ZONE 'UTC')::date "
        "WHERE run_day IS NULL"
    )
    op.alter_column("provider_conditional_runs", "run_day", nullable=False)
    op.drop_constraint(
        "uq_provider_conditional_run_model_sha",
        "provider_conditional_runs",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_provider_conditional_run_model_sha_day",
        "provider_conditional_runs",
        ["model_version", "code_sha", "run_day"],
    )
    op.create_index(
        "ix_provider_conditional_run_day",
        "provider_conditional_runs",
        ["run_day", "completed_at"],
    )

    # Old v1 terminal misses remain immutable historical facts. The new context policy
    # can legitimately recover D1-only partial snapshots, so allow exactly one v2 retry
    # outcome beside the original v1 record instead of deleting/re-writing history.
    op.drop_constraint(
        "uq_provider_context_terminal_miss_signal",
        "provider_signal_context_terminal_misses",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_provider_context_terminal_miss_signal_contract",
        "provider_signal_context_terminal_misses",
        ["signal_id", "contract_version"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_provider_context_terminal_miss_signal_contract",
        "provider_signal_context_terminal_misses",
        type_="unique",
    )
    # A downgrade is only safe when no signal has multiple contract-version misses.
    op.create_unique_constraint(
        "uq_provider_context_terminal_miss_signal",
        "provider_signal_context_terminal_misses",
        ["signal_id"],
    )

    op.drop_index("ix_provider_conditional_run_day", table_name="provider_conditional_runs")
    op.drop_constraint(
        "uq_provider_conditional_run_model_sha_day",
        "provider_conditional_runs",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_provider_conditional_run_model_sha",
        "provider_conditional_runs",
        ["model_version", "code_sha"],
    )
    op.drop_column("provider_conditional_runs", "run_day")
