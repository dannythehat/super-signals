"""Owner authentication, session and recovery acceptance checks."""

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
from app.security import hash_password, verify_password
from app.seed import seed_account, seed_owner

DATABASE_URL = os.getenv("DATABASE_URL")
API_ROOT = Path(__file__).resolve().parents[1]


def _alembic_config() -> Config:
    config = Config(str(API_ROOT / "alembic.ini"))
    if DATABASE_URL:
        config.set_main_option("sqlalchemy.url", DATABASE_URL)
    return config


@pytest.fixture()
def auth_client(monkeypatch: pytest.MonkeyPatch):
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")

    monkeypatch.setenv("SUPER_SIGNALS_ENV", "test")
    monkeypatch.setenv("SUPER_SIGNALS_COOKIE_SECURE", "false")
    monkeypatch.setenv("SUPER_SIGNALS_FINGERPRINT_SECRET", "test-fingerprint-secret")
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
            {"hash": hash_password("correct horse battery staple"), "id": owner.id},
        )
        session.commit()

    with TestClient(create_app()) as client:
        yield client, engine

    command.downgrade(config, "base")
    engine.dispose()
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


def test_password_hash_round_trip() -> None:
    encoded = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("wrong password", encoded)
    assert "correct horse" not in encoded


def test_owner_can_login_reach_protected_page_and_logout(auth_client) -> None:
    client, _ = auth_client
    login = client.post(
        "/auth/login",
        json={
            "email": "OWNER@example.com",
            "password": "correct horse battery staple",
        },
    )
    assert login.status_code == 200
    assert login.json()["role"] == "owner"
    assert login.json()["security"] == {
        "two_factor": "setup_required",
        "passkey": "setup_available",
    }
    assert "HttpOnly" in login.headers["set-cookie"]
    assert "SameSite=strict" in login.headers["set-cookie"]

    protected = client.get("/auth/me")
    assert protected.status_code == 200
    assert protected.json()["email"] == "owner@example.com"

    logout = client.post("/auth/logout")
    assert logout.status_code == 204
    assert client.get("/auth/me").status_code == 401


def test_missing_and_passwordless_accounts_are_rejected(auth_client) -> None:
    client, engine = auth_client
    missing = client.post(
        "/auth/login",
        json={"email": "missing@example.com", "password": "any password"},
    )

    with Session(engine) as session:
        seed_account(session, "waiting@example.com", "Waiting User", "user")

    passwordless = client.post(
        "/auth/login",
        json={"email": "waiting@example.com", "password": "any password"},
    )

    assert missing.status_code == 401
    assert passwordless.status_code == 401
    assert missing.json() == passwordless.json()


def test_invalid_and_missing_sessions_are_rejected(auth_client) -> None:
    client, _ = auth_client
    assert client.get("/auth/me").status_code == 401
    client.cookies.set("super_signals_session", "not-a-real-session")
    assert client.get("/auth/me").status_code == 401


def test_expired_session_is_rejected(auth_client) -> None:
    client, engine = auth_client
    login = client.post(
        "/auth/login",
        json={
            "email": "owner@example.com",
            "password": "correct horse battery staple",
        },
    )
    assert login.status_code == 200
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE auth_sessions
                SET created_at = now() - interval '2 hours',
                    expires_at = now() - interval '1 hour'
                WHERE revoked_at IS NULL
                """
            )
        )
    assert client.get("/auth/me").status_code == 401


def test_recovery_response_does_not_reveal_account_existence(auth_client) -> None:
    client, engine = auth_client
    existing = client.post("/auth/recovery", json={"email": "owner@example.com"})
    missing = client.post("/auth/recovery", json={"email": "missing@example.com"})

    assert existing.status_code == 202
    assert missing.status_code == 202
    assert existing.json() == missing.json()
    assert "token" not in existing.text.lower()

    with engine.connect() as connection:
        count = connection.scalar(text("SELECT count(*) FROM password_recovery_requests"))
    assert count == 1
