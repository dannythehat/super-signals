"""Make Provider Intelligence context recovery and learning refresh repeatable.

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
    # The original schema allowed one conditional run per source code SHA. That froze
    # learning for the lifetime of a deployment. Evidence is already content-addressed
    # by evidence_digest, so permit another append-only research snapshot only when the
    # usable PIT/OOS evidence has actually changed. No retrospective provider score table
    # participates in this identity.
    op.drop_constraint(
        "uq_provider_conditional_run_model_sha",
        "provider_conditional_runs",
        type_="unique",
    )
    op.create_index(
        "uq_provider_conditional_run_model_sha_evidence",
        "provider_conditional_runs",
        ["model_version", "code_sha", "evidence_digest"],
        unique=True,
        postgresql_where=sa.text("evidence_digest IS NOT NULL"),
    )

    # Old v1 terminal misses remain immutable historical facts. The new Provider Context
    # contract can legitimately reconsider the subset that failed under the old strict
    # completeness rule, so allow exactly one v2 outcome beside each original v1 row.
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

    op.drop_index(
        "uq_provider_conditional_run_model_sha_evidence",
        table_name="provider_conditional_runs",
    )
    op.create_unique_constraint(
        "uq_provider_conditional_run_model_sha",
        "provider_conditional_runs",
        ["model_version", "code_sha"],
    )
