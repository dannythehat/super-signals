"""Day 9 Telegram source-selection acceptance checks."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_engine, get_session_factory
from app.main import create_app
from app.models import AuditEvent, Message, Source, TelegramAccount
from app.routes.telegram_sources import provide_telegram_source_service
from app.security import hash_password
from app.seed import seed_owner
from app.telegram_crypto import TelegramSessionCipher
from app.telegram_source_gateway import (
    TelegramSelectableDialog,
    TelethonTelegramSourceGateway,
)
from app.telegram_source_service import TelegramSourceService, get_telegram_source_service

DATABASE_URL = os.getenv("DATABASE_URL")
API_ROOT = Path(__file__).resolve().parents[1]
TEST_FERNET_KEY = "SY6ZSyA-C-HLcoOd_Wy60cnF3wVElxxhhDxwPVkrQtA="
RAW_SESSION = "1-day-nine-test-session"


def _alembic_config() -> Config:
    config = Config(str(API_ROOT / "alembic.ini"))
    if DATABASE_URL:
        config.set_main_option("sqlalchemy.url", DATABASE_URL)
    return config


@dataclass
class FakeSourceGateway:
    listed_sessions: list[str] = field(default_factory=list)
    dialogs: list[object] = field(
        default_factory=lambda: [
            TelegramSelectableDialog(chat_id=-100111, title="Gold Signals", kind="channel"),
            TelegramSelectableDialog(chat_id=-222, title="Trading Room", kind="group"),
            SimpleNamespace(chat_id=777, title="Private Friend", kind="user"),
        ]
    )

    async def list_selectable_dialogs(self, session_string: str):
        self.listed_sessions.append(session_string)
        return list(self.dialogs)


@pytest.fixture()
def source_client(monkeypatch: pytest.MonkeyPatch):
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
        session.execute(
            text("UPDATE users SET password_hash = :hash WHERE id = :id"),
            {"hash": hash_password("correct horse battery staple"), "id": owner.id},
        )
        account = TelegramAccount(
            owner_user_id=owner.id,
            label="Primary signal reader",
            phone_number_e164="+359881234567",
            session_ciphertext=cipher.encrypt(RAW_SESSION),
            session_fingerprint=cipher.fingerprint(RAW_SESSION),
            status="connected",
        )
        session.add(account)
        session.commit()
        session.refresh(account)
        account_id = account.id

    gateway = FakeSourceGateway()
    service = TelegramSourceService(gateway, cipher)
    application = create_app()
    application.dependency_overrides[provide_telegram_source_service] = lambda: service

    with TestClient(application) as client:
        login = client.post(
            "/auth/login",
            json={
                "email": "owner@example.com",
                "password": "correct horse battery staple",
            },
        )
        assert login.status_code == 200
        yield client, engine, gateway, account_id

    command.downgrade(config, "base")
    engine.dispose()
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    get_telegram_source_service.cache_clear()


def test_gateway_rejects_private_users_and_keeps_groups_and_channels() -> None:
    private_user = SimpleNamespace(
        id=123,
        title="Alice",
        is_user=True,
        is_group=False,
        is_channel=False,
    )
    bot_chat = SimpleNamespace(
        id=456,
        title="Helper Bot",
        is_user=True,
        is_group=False,
        is_channel=False,
    )
    group = SimpleNamespace(
        id=-222,
        title="Trading Room",
        is_user=False,
        is_group=True,
        is_channel=False,
    )
    channel = SimpleNamespace(
        id=-100111,
        title="Gold Signals",
        is_user=False,
        is_group=False,
        is_channel=True,
    )
    supergroup = SimpleNamespace(
        id=-100333,
        title="VIP Supergroup",
        is_user=False,
        is_group=True,
        is_channel=True,
    )

    assert TelethonTelegramSourceGateway._to_selectable_dialog(private_user) is None
    assert TelethonTelegramSourceGateway._to_selectable_dialog(bot_chat) is None
    assert TelethonTelegramSourceGateway._to_selectable_dialog(group) == TelegramSelectableDialog(
        chat_id=-222, title="Trading Room", kind="group"
    )
    assert TelethonTelegramSourceGateway._to_selectable_dialog(channel) == TelegramSelectableDialog(
        chat_id=-100111, title="Gold Signals", kind="channel"
    )
    assert (
        TelethonTelegramSourceGateway._to_selectable_dialog(supergroup)
        == TelegramSelectableDialog(
            chat_id=-100333,
            title="VIP Supergroup",
            kind="group",
        )
    )


def test_discovery_is_read_only_and_private_chats_are_absent(source_client) -> None:
    client, engine, gateway, account_id = source_client

    response = client.get(f"/admin/telegram/sources/accounts/{account_id}/available")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert gateway.listed_sessions == [RAW_SESSION]
    assert response.json() == [
        {
            "chat_id": -100111,
            "title": "Gold Signals",
            "kind": "channel",
            "selected": False,
            "source_id": None,
            "status": None,
        },
        {
            "chat_id": -222,
            "title": "Trading Room",
            "kind": "group",
            "selected": False,
            "source_id": None,
            "status": None,
        },
    ]
    assert "Private Friend" not in response.text

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Source)) == 0
        assert session.scalar(select(func.count()).select_from(Message)) == 0


def test_source_must_be_explicitly_selected_and_stays_paused(source_client) -> None:
    client, engine, _, account_id = source_client

    rejected_private = client.post(
        f"/admin/telegram/sources/accounts/{account_id}/select",
        json={"chat_id": 777},
    )
    assert rejected_private.status_code == 404

    selected = client.post(
        f"/admin/telegram/sources/accounts/{account_id}/select",
        json={"chat_id": -100111},
    )
    assert selected.status_code == 201
    body = selected.json()
    assert body["selected"] is True
    assert body["status"] == "paused"
    assert body["kind"] == "channel"
    source_id = UUID(body["source_id"])

    with Session(engine) as session:
        source = session.get(Source, source_id)
        assert source is not None
        assert source.status == "paused"
        assert source.chat_title == "Gold Signals"
        assert session.scalar(select(func.count()).select_from(Message)) == 0
        audit = session.scalar(
            select(AuditEvent).where(AuditEvent.event_type == "telegram.source_selected")
        )
        assert audit is not None
        assert audit.payload["monitoring_started"] is False
        assert audit.payload["status"] == "paused"

    discovered = client.get(f"/admin/telegram/sources/accounts/{account_id}/available")
    selected_row = next(item for item in discovered.json() if item["chat_id"] == -100111)
    assert selected_row["selected"] is True
    assert selected_row["status"] == "paused"

    removed = client.post(
        f"/admin/telegram/sources/accounts/{account_id}/selected/{source_id}/remove"
    )
    assert removed.status_code == 200
    assert removed.json() == {
        "removed": True,
        "source_id": str(source_id),
        "monitoring_started": False,
    }

    with Session(engine) as session:
        source = session.get(Source, source_id)
        assert source is not None
        assert source.status == "revoked"
        assert session.scalar(select(func.count()).select_from(Message)) == 0
