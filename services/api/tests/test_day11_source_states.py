"""Day 11 acceptance checks for Testing, Live and Paused source states."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_engine, get_session_factory
from app.main import create_app
from app.models import AuditEvent, Message, Position, Source, TelegramAccount
from app.routes.telegram_sources import provide_telegram_source_service
from app.security import hash_password
from app.seed import seed_account, seed_owner
from app.telegram_crypto import TelegramSessionCipher
from app.telegram_source_service import TelegramSourceService, get_telegram_source_service

DATABASE_URL = os.getenv("DATABASE_URL")
API_ROOT = Path(__file__).resolve().parents[1]
TEST_FERNET_KEY = "SY6ZSyA-C-HLcoOd_Wy60cnF3wVElxxhhDxwPVkrQtA="


def _alembic_config() -> Config:
    config = Config(str(API_ROOT / "alembic.ini"))
    if DATABASE_URL:
        config.set_main_option("sqlalchemy.url", DATABASE_URL)
    return config


@dataclass
class UnusedGateway:
    async def list_selectable_dialogs(self, session_string: str):
        del session_string
        return []


def _login(client: TestClient, email: str, password: str) -> None:
    response = client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200


@pytest.fixture()
def day11_client(monkeypatch: pytest.MonkeyPatch):
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")

    monkeypatch.setenv("SUPER_SIGNALS_ENV", "test")
    monkeypatch.setenv("SUPER_SIGNALS_COOKIE_SECURE", "false")
    monkeypatch.setenv("SUPER_SIGNALS_FINGERPRINT_SECRET", "test-fingerprint-secret")
    monkeypatch.setenv("SUPER_SIGNALS_TELEGRAM_SESSION_KEYS", TEST_FERNET_KEY)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    get_telegram_source_service.cache_clear()

    config = _alembic_config()
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    engine = create_engine(DATABASE_URL, future=True)
    cipher = TelegramSessionCipher((TEST_FERNET_KEY,))

    with Session(engine) as session:
        owner = seed_owner(session, "owner@example.com", "Danny")
        trading_admin = seed_account(session, "rikke@example.com", "Rikke", "trading_admin")
        session.execute(
            text("UPDATE users SET password_hash = :hash WHERE id = :id"),
            {"hash": hash_password("owner password 123"), "id": owner.id},
        )
        session.execute(
            text("UPDATE users SET password_hash = :hash WHERE id = :id"),
            {"hash": hash_password("rikke password 123"), "id": trading_admin.id},
        )
        reader = TelegramAccount(
            owner_user_id=owner.id,
            label="Owner reader",
            phone_number_e164="+359881234567",
            session_ciphertext=cipher.encrypt("day11-owner-reader"),
            session_fingerprint=cipher.fingerprint("day11-owner-reader"),
            status="connected",
        )
        session.add(reader)
        session.flush()
        source = Source(
            telegram_account_id=reader.id,
            chat_id=-100111,
            chat_title="Gold Signals",
            source_alias="Gold Signals",
            status="paused",
            redistribution_permission_confirmed=False,
            created_by_user_id=owner.id,
        )
        session.add(source)
        session.commit()
        session.refresh(source)
        source_id = source.id
        owner_id = owner.id
        trading_admin_id = trading_admin.id

    service = TelegramSourceService(UnusedGateway(), cipher)
    application = create_app()
    application.dependency_overrides[provide_telegram_source_service] = lambda: service

    with TestClient(application) as client:
        yield client, engine, source_id, owner_id, trading_admin_id

    command.downgrade(config, "base")
    engine.dispose()
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    get_telegram_source_service.cache_clear()


def test_trading_admin_state_changes_are_immediate_audited_and_alert_owner(day11_client) -> None:
    client, engine, source_id, _, trading_admin_id = day11_client
    _login(client, "rikke@example.com", "rikke password 123")

    transitions = (
        ("paused", "testing"),
        ("testing", "live"),
        ("live", "paused"),
    )
    for previous_status, next_status in transitions:
        response = client.patch(
            f"/admin/telegram/sources/shared/{source_id}/status",
            json={"status": next_status},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["source_id"] == str(source_id)
        assert body["title"] == "Gold Signals"
        assert body["previous_status"] == previous_status
        assert body["status"] == next_status
        assert body["actor_display_name"] == "Rikke"
        assert body["actor_role"] == "trading_admin"
        assert body["monitoring_started"] is False
        assert body["live_trading_enabled"] is False
        assert body["changed_at"]

        with Session(engine) as session:
            assert session.get(Source, source_id).status == next_status
            assert session.scalar(select(func.count()).select_from(Message)) == 0
            assert session.scalar(select(func.count()).select_from(Position)) == 0

    denied_alerts = client.get("/admin/telegram/sources/owner-alerts")
    assert denied_alerts.status_code == 403

    with Session(engine) as session:
        changes = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.event_type == "telegram.source_status_changed")
            .order_by(AuditEvent.id.asc())
        ).all()
        assert len(changes) == 3
        assert all(event.actor_user_id == trading_admin_id for event in changes)
        assert [event.payload["previous_status"] for event in changes] == [
            "paused",
            "testing",
            "live",
        ]
        assert [event.payload["status"] for event in changes] == ["testing", "live", "paused"]
        assert all(event.payload["changed_at"] for event in changes)
        assert all(event.payload["monitoring_started"] is False for event in changes)
        assert all(event.payload["live_trading_enabled"] is False for event in changes)

        owner_alerts = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.event_type == "owner.alert.source_status_changed")
            .order_by(AuditEvent.id.asc())
        ).all()
        assert len(owner_alerts) == 3
        assert all(event.payload["audience"] == "owner" for event in owner_alerts)

    assert client.post("/auth/logout").status_code == 204
    _login(client, "owner@example.com", "owner password 123")
    alerts = client.get("/admin/telegram/sources/owner-alerts")
    assert alerts.status_code == 200
    assert [item["status"] for item in alerts.json()] == ["paused", "live", "testing"]
    assert all(item["actor_display_name"] == "Rikke" for item in alerts.json())
    assert all(item["title"] == "Gold Signals" for item in alerts.json())
    assert all(item["changed_at"] for item in alerts.json())


def test_owner_change_is_audited_without_generating_an_owner_self_alert(day11_client) -> None:
    client, engine, source_id, owner_id, _ = day11_client
    _login(client, "owner@example.com", "owner password 123")

    response = client.patch(
        f"/admin/telegram/sources/shared/{source_id}/status",
        json={"status": "testing"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "testing"
    assert response.json()["actor_role"] == "owner"

    with Session(engine) as session:
        change = session.scalar(
            select(AuditEvent).where(AuditEvent.event_type == "telegram.source_status_changed")
        )
        assert change is not None
        assert change.actor_user_id == owner_id
        assert change.payload["previous_status"] == "paused"
        assert change.payload["status"] == "testing"
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "owner.alert.source_status_changed")
            )
            == 0
        )

    alerts = client.get("/admin/telegram/sources/owner-alerts")
    assert alerts.status_code == 200
    assert alerts.json() == []


def test_revoked_is_not_an_operating_state(day11_client) -> None:
    client, _, source_id, _, _ = day11_client
    _login(client, "owner@example.com", "owner password 123")

    response = client.patch(
        f"/admin/telegram/sources/shared/{source_id}/status",
        json={"status": "revoked"},
    )
    assert response.status_code == 422
