"""One-shot live logical backup/restore proof for Day 39.

The backup exists only in the container's ephemeral /tmp filesystem and is
deleted after an isolated restore has been verified. Credential values are
never written to logs or audit payloads.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy.engine import URL, make_url

from app.config import get_settings
from app.db import get_session_factory
from app.models import AuditEvent


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


def _pg_environment(url: URL) -> dict[str, str]:
    environment = os.environ.copy()
    if url.password:
        environment["PGPASSWORD"] = url.password
    sslmode = url.query.get("sslmode")
    if sslmode:
        environment["PGSSLMODE"] = str(sslmode)
    return environment


def _pg_args(url: URL, database: str) -> list[str]:
    return [
        "--host",
        str(url.host),
        "--port",
        str(url.port or 5432),
        "--username",
        str(url.username),
        "--dbname",
        database,
    ]


def _table_counts(connection: psycopg.Connection) -> dict[str, int]:
    tables = [
        row[0]
        for row in connection.execute(
            """
            SELECT tablename
            FROM pg_tables
            WHERE schemaname = 'public'
            ORDER BY tablename
            """
        ).fetchall()
    ]
    counts: dict[str, int] = {}
    for table_name in tables:
        value = connection.execute(
            sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table_name))
        ).fetchone()
        counts[table_name] = int(value[0])
    return counts


def _identity_digest(connection: psycopg.Connection, table_name: str) -> str:
    value = connection.execute(
        sql.SQL(
            """
            SELECT md5(
                COALESCE(
                    string_agg(id::text, ',' ORDER BY id::text),
                    ''
                )
            )
            FROM {}
            """
        ).format(sql.Identifier(table_name))
    ).fetchone()
    return str(value[0])


def _verify_audit_mutation_blocked(connection: psycopg.Connection) -> bool:
    audit_count = int(
        connection.execute("SELECT count(*) FROM audit_events").fetchone()[0]
    )
    if audit_count == 0:
        raise RuntimeError("Restored audit log unexpectedly contains no rows.")

    try:
        with connection.transaction():
            connection.execute(
                """
                UPDATE audit_events
                SET event_type = event_type
                WHERE id = (SELECT max(id) FROM audit_events)
                """
            )
    except psycopg.Error as exc:
        if "append-only" not in str(exc):
            raise
        return True
    return False


def run_day39_backup_restore_acceptance() -> None:
    settings = get_settings()
    url = make_url(settings.database_url)
    primary_database = str(url.database or "")
    if not primary_database:
        raise RuntimeError("DATABASE_URL does not identify a database.")

    scratch_database = f"ss_day39_{uuid4().hex[:12]}"
    backup_path: Path | None = None
    scratch_created = False
    owner_id = None

    with tempfile.NamedTemporaryFile(
        prefix="super-signals-day39-",
        suffix=".dump",
        delete=False,
    ) as temporary:
        backup_path = Path(temporary.name)

    try:
        pg_environment = _pg_environment(url)
        with psycopg.connect(
            **_connection_kwargs(url, primary_database),
            autocommit=False,
        ) as primary:
            primary.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            )
            snapshot_id = str(
                primary.execute("SELECT pg_export_snapshot()").fetchone()[0]
            )
            primary_counts = _table_counts(primary)
            primary_version = str(
                primary.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            )
            primary_digests = {
                table: _identity_digest(primary, table)
                for table in ("messages", "signals", "positions", "audit_events")
            }
            owner_row = primary.execute(
                """
                SELECT u.id
                FROM users AS u
                JOIN user_roles AS ur ON ur.user_id = u.id
                JOIN roles AS r ON r.id = ur.role_id
                WHERE u.status = 'active' AND r.name = 'owner'
                ORDER BY u.created_at
                LIMIT 1
                """
            ).fetchone()
            owner_id = owner_row[0] if owner_row else None

            dump_command = [
                "pg_dump",
                *_pg_args(url, primary_database),
                "--format=custom",
                "--no-owner",
                "--no-privileges",
                "--snapshot",
                snapshot_id,
                "--file",
                str(backup_path),
            ]
            dump = subprocess.run(
                dump_command,
                env=pg_environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if dump.returncode != 0:
                raise RuntimeError("pg_dump failed during Day 39 backup proof.")

        backup_bytes = backup_path.stat().st_size
        if backup_bytes <= 0:
            raise RuntimeError("Day 39 logical backup file is empty.")
        backup_sha256 = hashlib.sha256(backup_path.read_bytes()).hexdigest()

        with psycopg.connect(
            **_connection_kwargs(url, "postgres"),
            autocommit=True,
        ) as admin:
            admin.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                    sql.Identifier(scratch_database)
                )
            )
            scratch_created = True

        restore_command = [
            "pg_restore",
            *_pg_args(url, scratch_database),
            "--no-owner",
            "--no-privileges",
            str(backup_path),
        ]
        restore = subprocess.run(
            restore_command,
            env=pg_environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if restore.returncode != 0:
            raise RuntimeError("pg_restore failed during Day 39 restore proof.")

        with psycopg.connect(
            **_connection_kwargs(url, scratch_database),
            autocommit=True,
        ) as restored:
            restored_counts = _table_counts(restored)
            restored_version = str(
                restored.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            )
            restored_digests = {
                table: _identity_digest(restored, table)
                for table in ("messages", "signals", "positions", "audit_events")
            }
            trigger_rows = restored.execute(
                """
                SELECT tgname, tgenabled
                FROM pg_trigger
                WHERE tgrelid = 'audit_events'::regclass
                  AND NOT tgisinternal
                ORDER BY tgname
                """
            ).fetchall()
            triggers_ok = trigger_rows == [
                ("audit_events_no_delete", "O"),
                ("audit_events_no_update", "O"),
            ]
            mutation_blocked = _verify_audit_mutation_blocked(restored)

        if primary_counts != restored_counts:
            raise RuntimeError("Restored table counts do not match the primary backup.")
        if primary_version != restored_version:
            raise RuntimeError("Restored Alembic version does not match the primary database.")
        if primary_digests != restored_digests:
            raise RuntimeError("Restored canonical identity digests do not match the primary.")
        if not triggers_ok or not mutation_blocked:
            raise RuntimeError("Restored audit append-only protection did not survive restore.")

        # Prove the isolated restore can be removed cleanly before recording the
        # acceptance event against the primary database.
        with psycopg.connect(
            **_connection_kwargs(url, "postgres"),
            autocommit=True,
        ) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                    sql.Identifier(scratch_database)
                )
            )
        scratch_created = False

        session_factory = get_session_factory()
        with session_factory() as session:
            session.add(
                AuditEvent(
                    actor_user_id=owner_id,
                    event_type="day39.backup_restore_verified",
                    entity_type="database",
                    payload={
                        "migration": primary_version,
                        "table_count": len(primary_counts),
                        "total_rows": sum(primary_counts.values()),
                        "backup_bytes": backup_bytes,
                        "backup_sha256": backup_sha256,
                        "row_counts_match": True,
                        "canonical_digests_match": True,
                        "audit_triggers_restored": True,
                        "audit_mutation_blocked_on_restore": True,
                        "scratch_database_removed_after_verification": True,
                        "trade_action_created": False,
                    },
                )
            )
            session.commit()
    finally:
        if scratch_created:
            try:
                with psycopg.connect(
                    **_connection_kwargs(url, "postgres"),
                    autocommit=True,
                ) as admin:
                    admin.execute(
                        sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                            sql.Identifier(scratch_database)
                        )
                    )
            except psycopg.Error:
                pass
        if backup_path is not None:
            backup_path.unlink(missing_ok=True)
