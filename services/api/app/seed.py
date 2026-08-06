"""Idempotent account and role seed commands."""

import argparse
import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_engine
from app.models import Role, User, UserRole

DEFAULT_ROLES = {
    "owner": "Full platform control, security, users, keys and roles.",
    "trading_admin": "Signal sources, tests, activity and emergency trading controls.",
    "user": "Personal MT5 connection, risk, automation, signals and performance.",
}

ROLE_DEFAULT_NAMES = {
    "owner": "Owner",
    "trading_admin": "Trading Admin",
    "user": "User",
}


def _ensure_roles(session: Session) -> dict[str, Role]:
    roles: dict[str, Role] = {}
    for name, description in DEFAULT_ROLES.items():
        role = session.scalar(select(Role).where(Role.name == name))
        if role is None:
            role = Role(name=name, description=description)
            session.add(role)
            session.flush()
        else:
            role.description = description
        roles[name] = role
    return roles


def seed_account(
    session: Session,
    email: str,
    display_name: str,
    role_name: str,
) -> User:
    normalized_email = email.strip().lower()
    if not normalized_email:
        raise ValueError("Account email is required.")
    if role_name not in DEFAULT_ROLES:
        raise ValueError(f"Unsupported role: {role_name}")

    roles = _ensure_roles(session)
    account = session.scalar(select(User).where(User.email == normalized_email))
    if account is None:
        account = User(
            email=normalized_email,
            display_name=display_name.strip() or ROLE_DEFAULT_NAMES[role_name],
            status="active",
        )
        session.add(account)
        session.flush()
    else:
        account.status = "active"
        if display_name.strip():
            account.display_name = display_name.strip()

    existing_link = session.scalar(
        select(UserRole).where(
            UserRole.user_id == account.id,
            UserRole.role_id == roles[role_name].id,
        )
    )
    if existing_link is None:
        session.add(UserRole(user_id=account.id, role_id=roles[role_name].id))

    session.commit()
    session.refresh(account)
    return account


def seed_owner(session: Session, email: str, display_name: str = "Owner") -> User:
    return seed_account(session, email, display_name, "owner")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed a Super Signals account.")
    parser.add_argument(
        "--email",
        default=os.getenv("SUPER_SIGNALS_OWNER_EMAIL"),
        help="Account email. Defaults to SUPER_SIGNALS_OWNER_EMAIL.",
    )
    parser.add_argument(
        "--display-name",
        default=os.getenv("SUPER_SIGNALS_OWNER_DISPLAY_NAME", "Owner"),
    )
    parser.add_argument(
        "--role",
        choices=tuple(DEFAULT_ROLES),
        default="owner",
    )
    args = parser.parse_args()

    if not args.email:
        parser.error("--email or SUPER_SIGNALS_OWNER_EMAIL is required")

    with Session(get_engine()) as session:
        account = seed_account(session, args.email, args.display_name, args.role)

    print(f"Account ready: {account.email} ({args.role})")


if __name__ == "__main__":
    main()
