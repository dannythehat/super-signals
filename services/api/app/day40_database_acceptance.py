"""One-shot isolated PostgreSQL regression for Day 40 acceptance.

The live primary database is never downgraded. This probe creates a randomly named
scratch database, runs the full Alembic head/base/head cycle in child processes whose
DATABASE_URL points only at that scratch database, proves the database safety contract,
drops the scratch database, then records non-secret evidence in the primary audit log.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_session_factory
from app.models import AuditEvent

API_ROOT = Path(__file__).resolve().parents[1]
CORE_TABLES = {
    "roles", "permissions", "role_permissions", "users", "user_roles",
    "invitations", "telegram_accounts", "sources", "messages", "signals",
    "positions", "audit_events", "auth_rate_limits", "broker_deals",
    "performance_account_snapshots", "performance_trade_outcomes",
    "performance_summaries", "telegram_live_board_state", "notification_events",
    "notification_reads", "telegram_notification_deliveries", "push_subscriptions",
    "push_notification_deliveries", "day34_summary_state",
}


def _connection_kwargs(url: URL, database: str) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "host": url.host,
        "port": url.port or 5432,
        "user": url.username,
        "password": url.password,
        "dbname": database,
    }
    sslmode = url.query.get("sslmode")
    if sslmode:
        kwargs["sslmode"] = sslmode
    return kwargs


def _database_url(url: URL, database: str) -> str:
    return url.set(database=database).render_as_string(hide_password=False)


def _run_alembic(database_url: str, action: str, target: str) -> None:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(API_ROOT / "alembic.ini"), action, target],
        cwd=API_ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Day 40 scratch Alembic {action} failed.")


def _permission_counts(engine) -> tuple[int, int, int, int]:
    with engine.connect() as connection:
        permission_count = int(connection.scalar(text("SELECT count(*) FROM permissions")) or 0)
        owner_count = int(connection.scalar(text("SELECT count(*) FROM role_permissions rp JOIN roles r ON r.id=rp.role_id WHERE r.name='owner'")) or 0)
        trading_admin_count = int(connection.scalar(text("SELECT count(*) FROM role_permissions rp JOIN roles r ON r.id=rp.role_id WHERE r.name='trading_admin'")) or 0)
        user_count = int(connection.scalar(text("SELECT count(*) FROM role_permissions rp JOIN roles r ON r.id=rp.role_id WHERE r.name='user'")) or 0)
        emergency_count = int(connection.scalar(text("SELECT count(*) FROM permissions WHERE code='emergency_stop.use'")) or 0)
    if (permission_count, owner_count, trading_admin_count, user_count, emergency_count) != (22, 22, 7, 8, 0):
        raise RuntimeError("Day 40 scratch permission matrix did not match the accepted contract.")
    return owner_count, trading_admin_count, user_count, emergency_count


def _audit_triggers(engine) -> tuple[bool, bool]:
    with engine.connect() as connection:
        triggers = list(connection.execute(text("""
            SELECT tgname, tgenabled
            FROM pg_trigger
            WHERE tgrelid='audit_events'::regclass AND NOT tgisinternal
            ORDER BY tgname
        """)).all())
    if triggers != [("audit_events_no_delete", "O"), ("audit_events_no_update", "O")]:
        raise RuntimeError("Day 40 scratch audit append-only triggers are missing or disabled.")
    return True, True


def _prove_audit_mutation_blocked(engine) -> tuple[bool, bool]:
    with Session(engine) as session:
        event = AuditEvent(event_type="day40.scratch_probe", entity_type="database", payload={"scratch": True})
        session.add(event)
        session.commit()
        event_id = event.id

    blocked: list[bool] = []
    for statement in (
        "UPDATE audit_events SET event_type='changed' WHERE id=:id",
        "DELETE FROM audit_events WHERE id=:id",
    ):
        was_blocked = False
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(text(statement), {"id": event_id})
            except DBAPIError as exc:
                was_blocked = "append-only" in str(exc)
            finally:
                transaction.rollback()
        blocked.append(was_blocked)
    if blocked != [True, True]:
        raise RuntimeError("Day 40 scratch audit mutation was not blocked.")
    return True, True


def _version(engine) -> str:
    with engine.connect() as connection:
        return str(connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one())


def run_day40_database_acceptance() -> None:
    settings = get_settings()
    primary_url = make_url(settings.database_url)
    if not primary_url.database:
        raise RuntimeError("DATABASE_URL does not identify a primary database.")

    scratch_database = f"ss_day40_{uuid4().hex[:12]}"
    scratch_created = False
    engine = None

    try:
        with psycopg.connect(**_connection_kwargs(primary_url, "postgres"), autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(scratch_database)))
            scratch_created = True

        scratch_url = _database_url(primary_url, scratch_database)
        engine = create_engine(scratch_url, future=True)

        _run_alembic(scratch_url, "upgrade", "head")
        if not CORE_TABLES <= set(inspect(engine).get_table_names()):
            raise RuntimeError("Day 40 scratch head migration is missing core tables.")
        if _version(engine) != "0024_day40_no_global_stop":
            raise RuntimeError("Day 40 scratch Alembic head is not the accepted revision.")

        owner_permissions, trading_admin_permissions, user_permissions, emergency_permission = _permission_counts(engine)
        update_trigger, delete_trigger = _audit_triggers(engine)
        update_blocked, delete_blocked = _prove_audit_mutation_blocked(engine)

        _run_alembic(scratch_url, "downgrade", "base")
        if CORE_TABLES & set(inspect(engine).get_table_names()):
            raise RuntimeError("Day 40 scratch downgrade left core application tables behind.")

        _run_alembic(scratch_url, "upgrade", "head")
        if not CORE_TABLES <= set(inspect(engine).get_table_names()):
            raise RuntimeError("Day 40 scratch migration reapply is missing core tables.")
        if _version(engine) != "0024_day40_no_global_stop":
            raise RuntimeError("Day 40 scratch reapply did not return to accepted head.")
        _permission_counts(engine)
        _audit_triggers(engine)
    finally:
        if engine is not None:
            engine.dispose()
        if scratch_created:
            with psycopg.connect(**_connection_kwargs(primary_url, "postgres"), autocommit=True) as admin:
                admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(scratch_database)))

    with get_session_factory()() as session:
        owner_id = session.scalar(text("""
            SELECT u.id FROM users u
            JOIN user_roles ur ON ur.user_id=u.id
            JOIN roles r ON r.id=ur.role_id
            WHERE u.status='active' AND r.name='owner'
            ORDER BY u.created_at LIMIT 1
        """))
        session.add(AuditEvent(
            actor_user_id=owner_id,
            event_type="day40.database_regression_passed",
            entity_type="database",
            payload={
                "migration": "0024_day40_no_global_stop",
                "head_tables_verified": len(CORE_TABLES),
                "clean_downgrade_to_base": True,
                "clean_reapply_to_head": True,
                "owner_permissions": owner_permissions,
                "trading_admin_permissions": trading_admin_permissions,
                "user_permissions": user_permissions,
                "global_emergency_permission": emergency_permission,
                "audit_update_trigger_enabled": update_trigger,
                "audit_delete_trigger_enabled": delete_trigger,
                "audit_update_blocked": update_blocked,
                "audit_delete_blocked": delete_blocked,
                "scratch_database_removed_after_verification": True,
                "trade_action_created": False,
            },
        ))
        session.commit()
