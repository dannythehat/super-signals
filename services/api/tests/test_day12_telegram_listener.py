"""Day 12 acceptance checks for exact-source Telegram message ingestion."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db import get_engine, get_session_factory
from app.main import create_app
from app.models import Message, Position, Signal, Source, TelegramAccount
from app.security import hash_password
from app.seed import seed_owner
from app.telegram_crypto import TelegramSessionCipher
from app.telegram_listener import CapturedTelegramMessage, TelegramListenerManager

DATABASE_URL = os.getenv("DATABASE_URL")
API_ROOT = Path(__file__).resolve().parents[1]
TEST_FERNET_KEY = "SY6ZSyA-C-HLcoOd_Wy60cnF3wVElxxhhDxwPVkrQtA="


def _alembic_config() -> Config:
    config = Config(str(API_ROOT / "alembic.ini"))
    if DATABASE_URL:
        config.set_main_option("sqlalchemy.url", DATABASE_URL)
    return config


def _link_reader(session: Session, source: Source, reader: TelegramAccount, owner_id) -> None:
    session.execute(
        text(
            """
            INSERT INTO source_reader_access (
                source_id, telegram_account_id, created_by_user_id
            ) VALUES (:source_id, :reader_id, :owner_id)
            """
        ),
        {"source_id": source.id, "reader_id": reader.id, "owner_id": owner_id},
    )


@pytest.fixture()
def day12_environment(monkeypatch: pytest.MonkeyPatch):
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")

    monkeypatch.setenv("SUPER_SIGNALS_ENV", "test")
    monkeypatch.setenv("SUPER_SIGNALS_COOKIE_SECURE", "false")
    monkeypatch.setenv("SUPER_SIGNALS_FINGERPRINT_SECRET", "test-fingerprint-secret")
    monkeypatch.setenv("SUPER_SIGNALS_TELEGRAM_SESSION_KEYS", TEST_FERNET_KEY)
    monkeypatch.setenv("SUPER_SIGNALS_TELEGRAM_LISTENER_ENABLED", "false")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    config = _alembic_config()
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    engine = create_engine(DATABASE_URL, future=True)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    cipher = TelegramSessionCipher((TEST_FERNET_KEY,))

    with Session(engine) as session:
        owner = seed_owner(session, "owner@example.com", "Danny")
        session.execute(
            text("UPDATE users SET password_hash = :hash WHERE id = :id"),
            {"hash": hash_password("owner password 123"), "id": owner.id},
        )

        connected = TelegramAccount(
            owner_user_id=owner.id,
            label="Connected reader",
            phone_number_e164="+359881234501",
            session_ciphertext=cipher.encrypt("connected-reader-session"),
            session_fingerprint=cipher.fingerprint("connected-reader-session"),
            status="connected",
        )
        disconnected = TelegramAccount(
            owner_user_id=owner.id,
            label="Old preferred reader",
            phone_number_e164="+359881234502",
            session_ciphertext=cipher.encrypt("disconnected-reader-session"),
            session_fingerprint=cipher.fingerprint("disconnected-reader-session"),
            status="disconnected",
        )
        session.add_all([connected, disconnected])
        session.flush()

        paused = Source(
            telegram_account_id=connected.id,
            chat_id=-10012001,
            chat_title="Paused Signals",
            source_alias="Paused Signals",
            status="paused",
            created_by_user_id=owner.id,
        )
        testing = Source(
            telegram_account_id=connected.id,
            chat_id=-10012002,
            chat_title="Testing Signals",
            source_alias="Testing Signals",
            status="testing",
            created_by_user_id=owner.id,
        )
        live_with_fallback = Source(
            telegram_account_id=disconnected.id,
            chat_id=-10012003,
            chat_title="Live Signals",
            source_alias="Live Signals",
            status="live",
            created_by_user_id=owner.id,
        )
        revoked = Source(
            telegram_account_id=connected.id,
            chat_id=-10012004,
            chat_title="Revoked Signals",
            source_alias="Revoked Signals",
            status="revoked",
            created_by_user_id=owner.id,
        )
        session.add_all([paused, testing, live_with_fallback, revoked])
        session.flush()
        _link_reader(session, paused, connected, owner.id)
        _link_reader(session, testing, connected, owner.id)
        _link_reader(session, live_with_fallback, disconnected, owner.id)
        _link_reader(session, live_with_fallback, connected, owner.id)
        _link_reader(session, revoked, connected, owner.id)
        session.commit()

        ids = {
            "owner": owner.id,
            "connected_reader": connected.id,
            "disconnected_reader": disconnected.id,
            "paused": paused.id,
            "testing": testing.id,
            "live": live_with_fallback.id,
            "revoked": revoked.id,
        }

    manager = TelegramListenerManager(
        api_id=12345,
        api_hash="a" * 32,
        cipher=cipher,
        session_factory=factory,
        refresh_seconds=5,
    )

    yield engine, manager, ids

    command.downgrade(config, "base")
    engine.dispose()
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


def test_listener_plan_contains_only_testing_and_live_with_connected_fallback(day12_environment) -> None:
    _, manager, ids = day12_environment

    plan = manager._load_plan()

    assert set(plan) == {ids["connected_reader"]}
    planned_sources = {source.source_id: source for source in plan[ids["connected_reader"]].sources}
    assert set(planned_sources) == {ids["testing"], ids["live"]}
    assert planned_sources[ids["testing"]].chat_id == -10012002
    assert planned_sources[ids["live"]].chat_id == -10012003
    assert ids["paused"] not in planned_sources
    assert ids["revoked"] not in planned_sources


def test_selected_message_is_stored_once_and_unselected_chat_never_enters_pipeline(day12_environment) -> None:
    engine, manager, ids = day12_environment
    now = datetime.now(UTC)

    selected = CapturedTelegramMessage(
        source_id=ids["testing"],
        chat_id=-10012002,
        telegram_message_id=7001,
        raw_text="BUY XAUUSD 2380 SL 2370 TP 2390",
        posted_at=now,
        reply_to_message_id=None,
        has_media=False,
        media_type=None,
    )
    assert manager._persist_message(selected) is True
    assert manager._persist_message(selected) is False

    # Defence in depth: even if an unexpected/unselected chat somehow reached
    # persistence with a selected source id, the chat-id recheck rejects it.
    unselected_chat = CapturedTelegramMessage(
        source_id=ids["testing"],
        chat_id=-10099999,
        telegram_message_id=7002,
        raw_text="THIS MUST NEVER ENTER",
        posted_at=now,
        reply_to_message_id=None,
        has_media=False,
        media_type=None,
    )
    assert manager._persist_message(unselected_chat) is False

    paused = CapturedTelegramMessage(
        source_id=ids["paused"],
        chat_id=-10012001,
        telegram_message_id=7003,
        raw_text="PAUSED MUST NEVER ENTER",
        posted_at=now,
        reply_to_message_id=None,
        has_media=False,
        media_type=None,
    )
    assert manager._persist_message(paused) is False

    with Session(engine) as session:
        stored = session.scalars(select(Message)).all()
        assert len(stored) == 1
        assert stored[0].source_id == ids["testing"]
        assert stored[0].telegram_message_id == 7001
        assert stored[0].ingestion_status == "received"
        assert stored[0].raw_payload["chat_id"] == -10012002
        assert session.scalar(select(func.count()).select_from(Signal)) == 0
        assert session.scalar(select(func.count()).select_from(Position)) == 0


def test_recent_message_log_is_authorised_and_contains_no_private_reader_data(day12_environment) -> None:
    engine, manager, ids = day12_environment
    captured = CapturedTelegramMessage(
        source_id=ids["live"],
        chat_id=-10012003,
        telegram_message_id=8001,
        raw_text="Gold signal test",
        posted_at=datetime.now(UTC),
        reply_to_message_id=7999,
        has_media=False,
        media_type=None,
    )
    assert manager._persist_message(captured) is True

    application = create_app()
    with TestClient(application) as client:
        unauthorised = client.get("/admin/telegram/messages/recent")
        assert unauthorised.status_code == 401

        login = client.post(
            "/auth/login",
            json={"email": "owner@example.com", "password": "owner password 123"},
        )
        assert login.status_code == 200
        response = client.get("/admin/telegram/messages/recent")
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["source_id"] == str(ids["live"])
        assert body[0]["source_title"] == "Live Signals"
        assert body[0]["telegram_message_id"] == 8001
        assert body[0]["raw_text"] == "Gold signal test"
        serialized = str(body[0]).lower()
        assert "phone" not in serialized
        assert "session" not in serialized
        assert "telegram_account" not in serialized

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Message)) == 1
