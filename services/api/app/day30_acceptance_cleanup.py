"""Temporary cleanup for Day 30 acceptance-only .invalid users."""

from sqlalchemy import text


def cleanup_day30_acceptance_records(session_factory) -> None:
    with session_factory() as session:
        user_ids = list(
            session.execute(
                text(
                    """
                    SELECT id
                    FROM users
                    WHERE email::text LIKE 'day30-%@example.invalid'
                    """
                )
            ).scalars()
        )
        if not user_ids:
            return
        session.execute(
            text(
                """
                UPDATE mt5_accounts
                SET status = 'revoked', updated_at = now()
                WHERE owner_user_id = ANY(:user_ids)
                """
            ),
            {"user_ids": user_ids},
        )
        session.execute(
            text(
                """
                UPDATE mt5_account_approvals
                SET status = 'revoked', updated_at = now()
                WHERE user_id = ANY(:user_ids)
                """
            ),
            {"user_ids": user_ids},
        )
        session.execute(
            text(
                """
                UPDATE users
                SET status = 'revoked', updated_at = now()
                WHERE id = ANY(:user_ids)
                """
            ),
            {"user_ids": user_ids},
        )
        session.commit()
