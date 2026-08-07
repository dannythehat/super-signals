"""Secure Telegram connection acceptance checks."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_engine, get_session_factory
from app.main import create_app
from app.routes.telegram_accounts import provide_telegram_connection_service
from app.security import hash_password
from app.seed import seed_account, seed_owner
from app.telegram_crypto import TelegramSessionCipher
from app.telegram_gateway import (
    TelegramAuthorizationResult,
    TelegramFlowNotFoundError,
    TelegramIdentity,
    TelegramPasswordInvalidError,
    TelegramQrAuthorization,
    TelegramSessionInvalidError,
)
from app.telegram_service import (
    TelegramConnectionService,
    get_telegram_connection_service,
)

DATABASE_URL = os.getenv("DATABASE_URL")
API_ROOT = Path(__file__).resolve().parents[1]
TEST_FERNET_KEY = "SY6ZSyA-C-HLcoOd_Wy60cnF3wVElxxhhDxwPVkrQtA="
RAW_SESSION = "1-test-portable-telegram-session"
IDENTITY = TelegramIdentity(
    user_id=123456789,
    phone_number_e164="+359881234567",
    username="test_reader",
)


def _alembic_config() -> Config:
    config = Config(str(API_ROOT / "alembic.ini"))
    if DATABASE_URL:
        config.set_main_option("sqlalchemy.url", DATABASE_URL)
    return config


@dataclass
class FakeTelegramGateway:
    require_password: bool = False
    valid_sessions: dict[str, TelegramIdentity] = field(
        default_factory=lambda: {RAW_SESSION: IDENTITY}
    )
    flows: set[UUID] = field(default_factory=set)
    checked_sessions: list[str] = field(default_factory=list)
    revoked_sessions: list[str] = field(default_factory=list)

    async def begin_qr_authorization(self, flow_id: UUID) -> TelegramQrAuthorization:
        self.flows.add(flow_id)
        return TelegramQrAuthorization(
            flow_id=flow_id,
            qr_url="tg://login?token=private-test-token",
            expires_at=datetime.now(UTC) + timedelta(minutes=2),
        )

    async def poll_qr_authorization(self, flow_id: UUID) -> TelegramAuthorizationResult:
        if flow_id not in self.flows:
            raise TelegramFlowNotFoundError("missing")
        if self.require_password:
            return TelegramAuthorizationResult(status="password_required")
        self.flows.remove(flow_id)
        return TelegramAuthorizationResult(
            status="connected",
            session_string=RAW_SESSION,
            identity=IDENTITY,
        )

    async def submit_password(self, flow_id: UUID, password: str) -> TelegramAuthorizationResult:
        if flow_id not in self.flows:
            raise TelegramFlowNotFoundError("missing")
        if password != "correct telegram password":
            raise TelegramPasswordInvalidError("wrong password")
        self.flows.remove(flow_id)
        return TelegramAuthorizationResult(
            status="connected",
            session_string=RAW_SESSION,
            identity=IDENTITY,
        )

    async def check_session(self, session_string: str) -> TelegramIdentity:
        self.checked_sessions.append(session_string)
        identity = self.valid_sessions.get(session_string)
        if identity is None:
            raise TelegramSessionInvalidError("invalid")
        return identity

    async def revoke_session(self, session_string: str) -> None:
        self.revoked_sessions.append(session_string)
        self.valid_sessions.pop(session_string, None)

    async def discard_flow(self, flow_id: UUID) -> None:
        self.flows.discard(flow_id)


@pytest.fixture()
def telegram_client(monkeypatch: pytest.MonkeyPatch):
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")

    monkeypatch.setenv("SUPER_SIGNALS_ENV", "test")
    monkeypatch.setenv("SUPER_SIGNALS_COOKIE_SECURE", "false")
    monkeypatch.setenv("SUPER_SIGNALS_FINGERPRINT_SECRET", "test-fingerprint-secret")
    monkeypatch.setenv("SUPER_SIGNALS_TELEGRAM_SESSION_KEYS", TEST_FERNET_KEY)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    get_telegram_connection_service.cache_clear()

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

    gateway = FakeTelegramGateway()
    cipher = TelegramSessionCipher((TEST_FERNET_KEY,))
    service = TelegramConnectionService(gateway, cipher)
    application = create_app()
    application.dependency_overrides[provide_telegram_connection_service] = lambda: service

    with TestClient(application) as client:
        login = client.post(
            "/auth/login",
            json={
                "email": "owner@example.com",
                "password": "correct horse battery staple",
            },
        )
        assert login.status_code == 200
        yield client, engine, application, gateway, cipher

    command.downgrade(config, "base")
    engine.dispose()
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    get_telegram_connection_service.cache_clear()


def _connect_account(client: TestClient) -> dict[str, object]:
    started = client.post(
        "/admin/telegram/accounts/authorize",
        json={"label": "Primary signal reader"},
    )
    assert started.status_code == 201
    assert started.headers["cache-control"] == "no-store"
    assert started.json()["qr_url"].startswith("tg://login?")

    completed = client.get(f"/admin/telegram/accounts/authorize/{started.json()['flow_id']}")
    assert completed.status_code == 200
    assert completed.json()["status"] == "connected"
    return completed.json()["account"]


def test_qr_connection_is_encrypted_survives_restart_and_disconnects(
    telegram_client,
) -> None:
    client, engine, application, _, cipher = telegram_client
    account = _connect_account(client)
    account_id = UUID(str(account["id"]))

    assert account["phone_hint"] == "+35***567"
    assert "+359881234567" not in client.get("/admin/telegram/accounts").text

    with engine.connect() as connection:
        stored = (
            connection.execute(
                text(
                    """
                SELECT session_ciphertext, session_fingerprint, status
                FROM telegram_accounts
                WHERE id = :id
                """
                ),
                {"id": account_id},
            )
            .mappings()
            .one()
        )
        audit_text = connection.scalar(
            text("SELECT string_agg(payload::text, ' ') FROM audit_events")
        )

    assert stored["status"] == "connected"
    assert RAW_SESSION.encode() not in stored["session_ciphertext"]
    assert cipher.decrypt(stored["session_ciphertext"]) == RAW_SESSION
    original_fingerprint = stored["session_fingerprint"]
    assert RAW_SESSION not in (audit_text or "")
    assert "+359881234567" not in (audit_text or "")

    restarted_gateway = FakeTelegramGateway()
    restarted_service = TelegramConnectionService(restarted_gateway, cipher)
    application.dependency_overrides[provide_telegram_connection_service] = lambda: (
        restarted_service
    )

    verified = client.post(f"/admin/telegram/accounts/{account_id}/verify")
    assert verified.status_code == 200
    assert verified.json()["status"] == "connected"
    assert restarted_gateway.checked_sessions == [RAW_SESSION]

    disconnected = client.post(f"/admin/telegram/accounts/{account_id}/disconnect")
    assert disconnected.status_code == 200
    assert disconnected.json() == {
        "disconnected": True,
        "server_session_destroyed": True,
        "remote_logout": True,
    }
    assert restarted_gateway.revoked_sessions == [RAW_SESSION]

    with engine.connect() as connection:
        destroyed = (
            connection.execute(
                text(
                    """
                SELECT session_ciphertext, session_fingerprint, status
                FROM telegram_accounts
                WHERE id = :id
                """
                ),
                {"id": account_id},
            )
            .mappings()
            .one()
        )

    destroyed_marker = cipher.decrypt(destroyed["session_ciphertext"])
    assert destroyed["status"] == "disconnected"
    assert destroyed_marker.startswith("destroyed:")
    assert destroyed_marker != RAW_SESSION
    assert destroyed["session_fingerprint"] != original_fingerprint
    assert client.post(f"/admin/telegram/accounts/{account_id}/verify").status_code == 409


def test_two_step_verification_password_is_not_echoed_or_stored(
    telegram_client,
) -> None:
    client, engine, _, gateway, _ = telegram_client
    gateway.require_password = True

    started = client.post(
        "/admin/telegram/accounts/authorize",
        json={"label": "Password protected reader"},
    )
    flow_id = started.json()["flow_id"]
    waiting = client.get(f"/admin/telegram/accounts/authorize/{flow_id}")
    assert waiting.json()["status"] == "password_required"

    rejected = client.post(
        f"/admin/telegram/accounts/authorize/{flow_id}/password",
        json={"password": "wrong"},
    )
    assert rejected.status_code == 400
    assert "wrong" not in rejected.text

    completed = client.post(
        f"/admin/telegram/accounts/authorize/{flow_id}/password",
        json={"password": "correct telegram password"},
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "connected"

    with engine.connect() as connection:
        audit_text = connection.scalar(
            text("SELECT string_agg(payload::text, ' ') FROM audit_events")
        )
    assert "correct telegram password" not in (audit_text or "")


def test_invited_user_cannot_manage_telegram_connections(telegram_client) -> None:
    client, engine, _, _, _ = telegram_client
    with Session(engine) as session:
        user = seed_account(session, "user@example.com", "Invited User", "user")
        session.execute(
            text("UPDATE users SET password_hash = :hash WHERE id = :id"),
            {"hash": hash_password("user password 123"), "id": user.id},
        )
        session.commit()

    assert client.post("/auth/logout").status_code == 204
    assert (
        client.post(
            "/auth/login",
            json={"email": "user@example.com", "password": "user password 123"},
        ).status_code
        == 200
    )

    denied = client.post(
        "/admin/telegram/accounts/authorize",
        json={"label": "Should fail"},
    )
    assert denied.status_code == 403

    with engine.connect() as connection:
        denial = connection.execute(
            text(
                """
                SELECT payload
                FROM audit_events
                WHERE event_type = 'permission.denied'
                ORDER BY id DESC
                LIMIT 1
                """
            )
        ).scalar_one()
    assert denial["permission"] == "sources.manage"
    assert denial["path"] == "/admin/telegram/accounts/authorize"
