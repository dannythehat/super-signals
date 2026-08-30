"""Merge subscription branch and activate the audited provider basket.

Revision ID: 0038_ranked_provider_activation
Revises: 0037_pause_gtmo_provider, 0029_member_subscriptions
Create Date: 2026-08-30

The risk-policy code in the same deployed branch reduces provider allocations before
these source states take effect. Only the canonical/faster GTMO feed is activated;
its slower mirror remains paused to prevent duplicate exposure.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0038_ranked_provider_activation"
down_revision: tuple[str, str] = (
    "0037_pause_gtmo_provider",
    "0029_member_subscriptions",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FX_CHAT_ID = -1001651583302
_TIG_CHAT_ID = -1003680830069
_GTMO_CANONICAL_CHAT_ID = -1001640332422
_GTMO_MIRROR_CHAT_ID = -1002068685216
_SURESHOT_CHAT_ID = -1001588519179
_UNITED_KINGS_CHAT_ID = -1002176701424


def upgrade() -> None:
    op.execute(
        f"""
        UPDATE sources
        SET status = CASE
                WHEN chat_id IN (
                    {_FX_CHAT_ID},
                    {_TIG_CHAT_ID},
                    {_GTMO_CANONICAL_CHAT_ID},
                    {_SURESHOT_CHAT_ID},
                    {_UNITED_KINGS_CHAT_ID}
                ) THEN 'testing'
                WHEN chat_id = {_GTMO_MIRROR_CHAT_ID} THEN 'paused'
                ELSE status
            END,
            updated_at = now()
        WHERE chat_id IN (
            {_FX_CHAT_ID},
            {_TIG_CHAT_ID},
            {_GTMO_CANONICAL_CHAT_ID},
            {_GTMO_MIRROR_CHAT_ID},
            {_SURESHOT_CHAT_ID},
            {_UNITED_KINGS_CHAT_ID}
        )
          AND status <> 'revoked'
        """
    )


def downgrade() -> None:
    # Restore only sources newly activated by this revision. FX and TIG were already
    # Testing before this migration; the GTMO mirror was already Paused.
    op.execute(
        f"""
        UPDATE sources
        SET status = 'paused', updated_at = now()
        WHERE chat_id IN (
            {_GTMO_CANONICAL_CHAT_ID},
            {_SURESHOT_CHAT_ID},
            {_UNITED_KINGS_CHAT_ID}
        )
          AND status = 'testing'
        """
    )
