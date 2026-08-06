"""Idempotent owner-account seed command."""

import argparse
import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_engine
from app.models import Role, User, UserRole

DEFAULT_ROLES = {
    "owner": "Full platform control, security, users, keys and roles.",
    "trading_admin": "Signal sources, tests, activity and emergency trading controls.",
}


def seed_owner(session: Session, email: str, display_name: str = "Owner") -> User:
    normalized_email = email.strip().lower()
    if not normalized_email:
        raise ValueError("Owner email is required.")

    roles: dict[str, Role] = {}
    for name, description in DEFAULT_ROLES.items():
        role = session.scalar(select(Role).where(Role.name == name))
        if role is None:
            role = Role(name=name, description=description)
            session.add(role)
            session.flush()
        roles[name] = role

    owner = session.scalar(select(User).where(User.email == normalized_email))
    if owner is None:
        owner = User(
            email=normalized_email,
            display_name=display_name.strip() or "Owner",
            status="active",
        )
        session.add(owner)
        session.flush()
    else:
        owner.status = "active"
        if display_name.strip():
            owner.display_name = display_name.strip()

    existing_link = session.scalar(
        select(UserRole).where(
            UserRole.user_id == owner.id,
            UserRole.role_id == roles["owner"].id,
        )
    )
    if existing_link is None:
        session.add(UserRole(user_id=owner.id, role_id=roles["owner"].id))

    session.commit()
    session.refresh(owner)
    return owner


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the initial Super Signals owner.")
    parser.add_argument(
        "--email",
        default=os.getenv("SUPER_SIGNALS_OWNER_EMAIL"),
        help="Owner email. Defaults to SUPER_SIGNALS_OWNER_EMAIL.",
    )
    parser.add_argument(
        "--display-name",
        default=os.getenv("SUPER_SIGNALS_OWNER_DISPLAY_NAME", "Owner"),
    )
    args = parser.parse_args()

    if not args.email:
        parser.error("--email or SUPER_SIGNALS_OWNER_EMAIL is required")

    with Session(get_engine()) as session:
        owner = seed_owner(session, args.email, args.display_name)

    print(f"Owner account ready: {owner.email}")


if __name__ == "__main__":
    main()
