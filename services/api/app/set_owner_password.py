"""Set or rotate the owner password without exposing it in source control."""

from __future__ import annotations

import argparse
import getpass

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_engine
from app.security import hash_password


def set_owner_password(session: Session, email: str, password: str) -> None:
    result = session.execute(
        text(
            """
            UPDATE users AS u
            SET password_hash = :password_hash,
                updated_at = now()
            WHERE lower(u.email::text) = lower(:email)
              AND u.status = 'active'
              AND EXISTS (
                  SELECT 1
                  FROM user_roles AS ur
                  JOIN roles AS r ON r.id = ur.role_id
                  WHERE ur.user_id = u.id
                    AND r.name = 'owner'
              )
            """
        ),
        {"email": email.strip(), "password_hash": hash_password(password)},
    )
    if result.rowcount != 1:
        session.rollback()
        raise ValueError("Active owner account not found.")
    session.execute(
        text(
            """
            UPDATE auth_sessions AS s
            SET revoked_at = COALESCE(s.revoked_at, now())
            WHERE s.user_id = (
                SELECT id FROM users WHERE lower(email::text) = lower(:email)
            )
            """
        ),
        {"email": email.strip()},
    )
    session.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Set the Super Signals owner password.")
    parser.add_argument("--email", required=True)
    args = parser.parse_args()
    password = getpass.getpass("New owner password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        parser.error("Passwords do not match.")

    with Session(get_engine()) as session:
        set_owner_password(session, args.email, password)
    print("Owner password updated and existing sessions revoked.")


if __name__ == "__main__":
    main()
