"""Integration checks for the PostgreSQL schema and owner seed."""

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.models import AuditEvent, Role, User, UserRole
from app.seed import seed_owner

DATABASE_URL = os.getenv("DATABASE_URL")
API_ROOT = Path(__file__).resolve().parents[1]
CORE_TABLES = {
    "roles",
    "users",
    "user_roles",
    "invitations",
    "telegram_accounts",
    "sources",
    "messages",
    "signals",
    "positions",
    "audit_events",
}


def _alembic_config() -> Config:
    config = Config(str(API_ROOT / "alembic.ini"))
    if DATABASE_URL:
        config.set_main_option("sqlalchemy.url", DATABASE_URL)
    return config


@pytest.fixture(scope="module")
def migrated_engine():
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")

    from sqlalchemy import create_engine

    engine = create_engine(DATABASE_URL, future=True)
    config = _alembic_config()
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    yield engine
    command.downgrade(config, "base")
    engine.dispose()


def test_migration_creates_all_core_tables(migrated_engine) -> None:
    assert CORE_TABLES <= set(inspect(migrated_engine).get_table_names())


def test_relationships_and_key_unique_constraints_exist(migrated_engine) -> None:
    inspector = inspect(migrated_engine)

    source_foreign_keys = {
        foreign_key["referred_table"]
        for foreign_key in inspector.get_foreign_keys("sources")
    }
    position_foreign_keys = {
        foreign_key["referred_table"]
        for foreign_key in inspector.get_foreign_keys("positions")
    }
    message_unique_names = {
        constraint["name"] for constraint in inspector.get_unique_constraints("messages")
    }
    position_unique_names = {
        constraint["name"] for constraint in inspector.get_unique_constraints("positions")
    }

    assert source_foreign_keys == {"telegram_accounts", "users"}
    assert position_foreign_keys == {"signals", "users"}
    assert "uq_messages_source_telegram_id" in message_unique_names
    assert "uq_positions_signal_user_tp" in position_unique_names


def test_owner_seed_is_idempotent_and_assigns_owner_role(migrated_engine) -> None:
    with Session(migrated_engine) as session:
        first = seed_owner(session, "OWNER@example.com", "Danny")
        first_id = first.id

    with Session(migrated_engine) as session:
        second = seed_owner(session, "owner@example.com", "Danny")
        assert second.id == first_id

        owner_role = session.scalar(select(Role).where(Role.name == "owner"))
        assert owner_role is not None
        link = session.scalar(
            select(UserRole).where(
                UserRole.user_id == second.id,
                UserRole.role_id == owner_role.id,
            )
        )
        assert link is not None


def test_case_insensitive_email_uniqueness_is_enforced(migrated_engine) -> None:
    with Session(migrated_engine) as session:
        session.add(User(email="owner@EXAMPLE.com", status="active"))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_audit_events_are_append_only(migrated_engine) -> None:
    with Session(migrated_engine) as session:
        event = AuditEvent(
            event_type="day4.test",
            entity_type="schema",
            payload={"verified": True},
        )
        session.add(event)
        session.commit()
        event_id = event.id

    with migrated_engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(DBAPIError, match="append-only"):
            connection.execute(
                text("UPDATE audit_events SET event_type = 'changed' WHERE id = :id"),
                {"id": event_id},
            )
        transaction.rollback()


def test_clean_rollback_and_reapply(migrated_engine) -> None:
    config = _alembic_config()
    command.downgrade(config, "base")
    remaining = set(inspect(migrated_engine).get_table_names())
    assert CORE_TABLES.isdisjoint(remaining)

    command.upgrade(config, "head")
    assert CORE_TABLES <= set(inspect(migrated_engine).get_table_names())
