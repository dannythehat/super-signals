"""Role access matrix and audited denial acceptance checks."""

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
from app.seed import seed_account

DATABASE_URL = os.getenv("DATABASE_URL")
API_ROOT = Path(__file__).resolve().parents[1]
PASSWORDS = {
    "owner": "owner secure password",
    "trading_admin": "trading admin secure password",
    "user": "user secure password",
}
EMAILS = {
    "owner": "owner@example.com",
    "trading_admin": "trading@example.com",
    "user": "user@example.com",
}

ACCESS_MATRIX = (
    ("GET", "/access/owner/users", {"owner"}, "users.manage"),
    ("GET", "/access/owner/security", {"owner"}, "security.manage"),
    (
        "GET",
        "/access/trading/sources",
        {"owner", "trading_admin"},
        "sources.manage",
    ),
    (
        "GET",
        "/access/trading/activity",
        {"owner", "trading_admin"},
        "activity.view",
    ),
    (
        "GET",
        "/access/trading/emergency-stop",
        {"owner", "trading_admin"},
        "emergency_stop.use",
    ),
    ("GET", "/access/user/account", {"owner", "user"}, "account.connect"),
    ("GET", "/access/user/automation", {"owner", "user"}, "automation.toggle"),
    ("GET", "/access/user/performance", {"owner", "user"}, "performance.view"),
)


def _alembic_config() -> Config:
    config = Config(str(API_ROOT / "alembic.ini"))
    if DATABASE_URL:
        config.set_main_option("sqlalchemy.url", DATABASE_URL)
    return config


@pytest.fixture(scope="module")
def role_environment(monkeypatch: pytest.MonkeyPatch):
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
        accounts = {
            "owner": seed_account(session, EMAILS["owner"], "Danny", "owner"),
            "trading_admin": seed_account(
                session,
                EMAILS["trading_admin"],
                "Trading Admin",
                "trading_admin",
            ),
            "user": seed_account(session, EMAILS["user"], "Invited User", "user"),
        }
        for role, account in accounts.items():
            session.execute(
                text("UPDATE users SET password_hash = :hash WHERE id = :id"),
                {"hash": hash_password(PASSWORDS[role]), "id": account.id},
            )
        session.commit()

    yield create_app(), engine

    command.downgrade(config, "base")
    engine.dispose()
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


def _login(client: TestClient, role: str) -> dict:
    response = client.post(
        "/auth/login",
        json={"email": EMAILS[role], "password": PASSWORDS[role]},
    )
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize("role", ("owner", "trading_admin", "user"))
def test_login_returns_role_specific_permissions(role_environment, role: str) -> None:
    app, _ = role_environment
    with TestClient(app) as client:
        identity = _login(client, role)

    assert identity["role"] == role
    assert identity["role_label"]
    assert identity["permissions"]
    section_keys = {section["key"] for section in identity["sections"]}
    if role == "owner":
        assert section_keys == {"owner", "trading", "user"}
    elif role == "trading_admin":
        assert section_keys == {"trading"}
    else:
        assert section_keys == {"user"}


@pytest.mark.parametrize("role", ("owner", "trading_admin", "user"))
@pytest.mark.parametrize(
    ("method", "path", "allowed_roles", "permission"),
    ACCESS_MATRIX,
)
def test_every_role_is_limited_to_its_allowed_actions(
    role_environment,
    role: str,
    method: str,
    path: str,
    allowed_roles: set[str],
    permission: str,
) -> None:
    app, _ = role_environment
    with TestClient(app) as client:
        _login(client, role)
        response = client.request(method, path)

    if role in allowed_roles:
        assert response.status_code == 200
        assert response.json()["allowed"] is True
        assert response.json()["permission"] == permission
    else:
        assert response.status_code == 403
        assert response.json()["detail"] == {
            "code": "permission_denied",
            "message": "You do not have permission to perform this action.",
            "permission": permission,
        }


def test_forbidden_request_is_audited(role_environment) -> None:
    app, engine = role_environment
    with TestClient(app) as client:
        identity = _login(client, "user")
        response = client.get(
            "/access/owner/security",
            headers={"X-Request-ID": "cd1a9d12-faa6-4617-9ba5-02bcb43eb66b"},
        )

    assert response.status_code == 403
    with engine.connect() as connection:
        event = (
            connection.execute(
                text(
                    """
                    SELECT actor_user_id, event_type, entity_type, payload, request_id
                    FROM audit_events
                    WHERE event_type = 'permission.denied'
                      AND actor_user_id = :actor_user_id
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ),
                {"actor_user_id": identity["id"]},
            )
            .mappings()
            .one()
        )

    assert str(event["actor_user_id"]) == identity["id"]
    assert event["entity_type"] == "permission"
    assert event["payload"] == {
        "permission": "security.manage",
        "role": "user",
        "method": "GET",
        "path": "/access/owner/security",
    }
    assert str(event["request_id"]) == "cd1a9d12-faa6-4617-9ba5-02bcb43eb66b"
