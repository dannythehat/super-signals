"""Day 29 owner invitations and single-use registration acceptance."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_engine, get_session_factory
from app.main import create_app
from app.security import hash_password
from app.seed import seed_owner

DATABASE_URL = os.getenv("DATABASE_URL")
API_ROOT = Path(__file__).resolve().parents[1]
OWNER_PASSWORD = "correct horse battery staple"


def _alembic_config() -> Config:
    config = Config(str(API_ROOT / "alembic.ini"))
    if DATABASE_URL:
        config.set_main_option("sqlalchemy.url", DATABASE_URL)
    return config


@pytest.fixture()
def invitation_client(monkeypatch: pytest.MonkeyPatch):
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")

    monkeypatch.setenv("SUPER_SIGNALS_ENV", "test")
    monkeypatch.setenv("SUPER_SIGNALS_COOKIE_SECURE", "false")
    monkeypatch.setenv("SUPER_SIGNALS_FINGERPRINT_SECRET", "day29-test-secret")
    monkeypatch.setenv("SUPER_SIGNALS_TELEGRAM_LISTENER_ENABLED", "false")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    config = _alembic_config()
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    engine = create_engine(DATABASE_URL, future=True)

    with Session(engine) as session:
        owner = seed_owner(session, "owner@example.com", "Danny")
        session.execute(
            text("UPDATE users SET password_hash = :hash WHERE id = :id"),
            {"hash": hash_password(OWNER_PASSWORD), "id": owner.id},
        )
        session.commit()

    with TestClient(create_app()) as client:
        login = client.post(
            "/auth/login",
            json={"email": "owner@example.com", "password": OWNER_PASSWORD},
        )
        assert login.status_code == 200
        yield client, engine

    command.downgrade(config, "base")
    engine.dispose()
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


def _create_invite(client: TestClient, email: str) -> dict:
    response = client.post(
        "/admin/accounts/invitations",
        json={"email": email, "expires_in_hours": 24},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _register(client: TestClient, *, email: str, access_key: str):
    return client.post(
        "/auth/register",
        json={
            "email": email,
            "access_key": access_key,
            "password": "member password 12345",
            "display_name": "Test Member",
        },
    )


def test_correct_email_and_key_register_once_and_key_is_never_stored_plaintext(
    invitation_client,
) -> None:
    client, engine = invitation_client
    invitation = _create_invite(client, "member@example.com")
    raw_key = invitation["access_key"]

    with engine.connect() as connection:
        stored_hash = connection.scalar(
            text("SELECT key_hash FROM invitations WHERE id = :id"),
            {"id": invitation["id"]},
        )
        assert stored_hash != raw_key
        assert raw_key not in str(stored_hash)

    wrong_email = _register(
        client,
        email="someone-else@example.com",
        access_key=raw_key,
    )
    assert wrong_email.status_code == 400

    success = _register(
        client,
        email="MEMBER@example.com",
        access_key=raw_key,
    )
    assert success.status_code == 201
    assert success.json()["email"] == "member@example.com"

    reused = _register(
        client,
        email="member@example.com",
        access_key=raw_key,
    )
    assert reused.status_code == 400
    assert reused.json() == wrong_email.json()

    with engine.connect() as connection:
        account = connection.execute(
            text(
                """
                SELECT u.status, r.name AS role_name, i.used_at
                FROM users AS u
                JOIN user_roles AS ur ON ur.user_id = u.id
                JOIN roles AS r ON r.id = ur.role_id
                JOIN invitations AS i ON lower(i.email::text) = lower(u.email::text)
                WHERE lower(u.email::text) = 'member@example.com'
                """
            )
        ).mappings().one()
        assert account["status"] == "active"
        assert account["role_name"] == "user"
        assert account["used_at"] is not None

        reasons = connection.execute(
            text(
                """
                SELECT payload->>'reason'
                FROM audit_events
                WHERE event_type = 'access.invitation_registration_rejected'
                ORDER BY id
                """
            )
        ).scalars().all()
        assert reasons == ["wrong_email", "reused_key"]
        success_count = connection.scalar(
            text(
                "SELECT count(*) FROM audit_events WHERE event_type = 'access.invitation_registered'"
            )
        )
        assert success_count == 1


def test_expired_and_revoked_keys_are_rejected_and_audited(invitation_client) -> None:
    client, engine = invitation_client

    expired = _create_invite(client, "expired@example.com")
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE invitations SET expires_at = now() - interval '1 second' WHERE id = :id"),
            {"id": expired["id"]},
        )
    expired_response = _register(
        client,
        email="expired@example.com",
        access_key=expired["access_key"],
    )
    assert expired_response.status_code == 400

    revoked = _create_invite(client, "revoked@example.com")
    revoke_response = client.post(
        f"/admin/accounts/invitations/{revoked['id']}/revoke"
    )
    assert revoke_response.status_code == 200
    revoked_response = _register(
        client,
        email="revoked@example.com",
        access_key=revoked["access_key"],
    )
    assert revoked_response.status_code == 400
    assert revoked_response.json() == expired_response.json()

    with engine.connect() as connection:
        reasons = set(
            connection.execute(
                text(
                    """
                    SELECT payload->>'reason'
                    FROM audit_events
                    WHERE event_type = 'access.invitation_registration_rejected'
                    """
                )
            ).scalars().all()
        )
        assert {"expired_key", "revoked_key"}.issubset(reasons)
        revoked_audit = connection.scalar(
            text(
                "SELECT count(*) FROM audit_events WHERE event_type = 'access.invitation_revoked'"
            )
        )
        assert revoked_audit == 1
