"""Day 14 acceptance checks for restart-safe source state and Telegram privacy."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db import get_engine, get_session_factory
from app.models import Message, Position, Signal, Source, TelegramAccount
from app.seed import seed_owner
from app.telegram_crypto import TelegramSessionCipher
from app.telegram_listener import CapturedTelegramMessage
from app.telegram_listener_day14 import Day14TelegramListenerManager
from app.telegram_source_gateway import TelethonTelegramSourceGateway
from app.telegram_source_service_day14 import Day14TelegramSourceService

DATABASE_URL = os.getenv("DATABASE_URL")
API_ROOT = Path(__file__).resolve().parents[1]
TEST_FERNET_KEY = "SY6ZSyA-C-HLcoOd_Wy60cnF3wVElxxhhDxwPVkrQtA="


def _alembic_config() -> Config:
    config = Config(str(API_ROOT / "alembic.ini"))
    if DATABASE_URL:
        config.set_main_option("sqlalchemy.url", DATABASE_URL)
    return config


@pytest.fixture()
def day14_environment(monkeypatch: pytest.MonkeyPatch):
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
        reader = TelegramAccount(
            owner_user_id=owner.id,
            label="Primary reader",
            phone_number_e164="+359881234501",
            session_ciphertext=cipher.encrypt("day14-primary-session"),
            session_fingerprint=cipher.fingerprint("day14-primary-session"),
            status="connected",
        )
        fallback = TelegramAccount(
            owner_user_id=owner.id,
            label="Fallback reader",
            phone_number_e164="+359881234502",
            session_ciphertext=cipher.encrypt("day14-fallback-session"),
            session_fingerprint=cipher.fingerprint("day14-fallback-session"),
            status="connected",
        )
        session.add_all([reader, fallback])
        session.flush()
        source = Source(
            telegram_account_id=reader.id,
            chat_id=-10014001,
            chat_title="Day 14 Signals",
            source_alias="Day 14 Signals",
            status="testing",
            created_by_user_id=owner.id,
        )
        session.add(source)
        session.flush()
        session.execute(
            text(
                """
                INSERT INTO source_reader_access (source_id, telegram_account_id, created_by_user_id)
                VALUES (:source_id, :reader_id, :owner_id)
                """
            ),
            {"source_id": source.id, "reader_id": reader.id, "owner_id": owner.id},
        )
        session.commit()
        ids = {
            "owner": owner.id,
            "reader": reader.id,
            "fallback": fallback.id,
            "source": source.id,
        }

    service = Day14TelegramSourceService(gateway=object(), cipher=cipher)  # type: ignore[arg-type]
    manager = Day14TelegramListenerManager(
        api_id=12345,
        api_hash="a" * 32,
        cipher=cipher,
        session_factory=factory,
        refresh_seconds=5,
    )
    yield engine, service, manager, ids

    command.downgrade(config, "base")
    engine.dispose()
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


def test_personal_user_dialogs_are_never_selectable() -> None:
    direct_user = SimpleNamespace(
        is_user=True,
        is_group=False,
        is_channel=False,
        id=123,
        title="Private person",
    )
    misleading_user = SimpleNamespace(
        is_user=True,
        is_group=True,
        is_channel=False,
        id=124,
        title="Still private",
    )
    saved_messages_like = SimpleNamespace(
        is_user=False,
        is_group=False,
        is_channel=False,
        id=125,
        title="Saved Messages",
    )
    assert TelethonTelegramSourceGateway._to_selectable_dialog(direct_user) is None
    assert TelethonTelegramSourceGateway._to_selectable_dialog(misleading_user) is None
    assert TelethonTelegramSourceGateway._to_selectable_dialog(saved_messages_like) is None


def test_last_reader_removal_preserves_source_and_reports_disconnected(day14_environment) -> None:
    engine, service, manager, ids = day14_environment
    actor = {"id": ids["owner"], "role": "owner", "display_name": "Danny"}

    with Session(engine) as session:
        before = service.list_shared_sources(session)
        assert len(before) == 1
        assert before[0].connection_status == "connected"
        assert before[0].connected_reader_count == 1

        service.unselect_source(
            session,
            actor=actor,
            account_id=ids["reader"],
            source_id=ids["source"],
        )

    with Session(engine) as session:
        source = session.get(Source, ids["source"])
        assert source is not None
        assert source.status == "testing"
        after = service.list_shared_sources(session)
        assert len(after) == 1
        assert after[0].connection_status == "disconnected"
        assert after[0].connected_reader_count == 0

    # A fresh listener-manager plan is rebuilt from PostgreSQL and has no worker
    # for the disconnected source. This is the same path used after service restart.
    assert manager._load_plan() == {}


def test_fallback_reader_keeps_one_logical_source_connected(day14_environment) -> None:
    engine, service, manager, ids = day14_environment
    actor = {"id": ids["owner"], "role": "owner", "display_name": "Danny"}

    with Session(engine) as session:
        session.execute(
            text(
                """
                INSERT INTO source_reader_access (source_id, telegram_account_id, created_by_user_id)
                VALUES (:source_id, :reader_id, :owner_id)
                """
            ),
            {
                "source_id": ids["source"],
                "reader_id": ids["fallback"],
                "owner_id": ids["owner"],
            },
        )
        session.commit()
        service.unselect_source(
            session,
            actor=actor,
            account_id=ids["reader"],
            source_id=ids["source"],
        )

    with Session(engine) as session:
        source = session.get(Source, ids["source"])
        assert source is not None
        assert source.telegram_account_id == ids["fallback"]
        reliability = service.list_shared_sources(session)
        assert len(reliability) == 1
        assert reliability[0].connection_status == "connected"
        assert reliability[0].connected_reader_count == 1

    plan = manager._load_plan()
    assert set(plan) == {ids["fallback"]}
    assert len(plan[ids["fallback"]].sources) == 1
    assert plan[ids["fallback"]].sources[0].source_id == ids["source"]


def test_unselected_chat_payload_cannot_enter_message_table(day14_environment) -> None:
    engine, _service, manager, ids = day14_environment
    captured = CapturedTelegramMessage(
        source_id=ids["source"],
        chat_id=999999999,
        telegram_message_id=14001,
        raw_text="PRIVATE MESSAGE MUST NOT BE STORED",
        posted_at=datetime.now(UTC),
        reply_to_message_id=None,
        has_media=False,
        media_type=None,
    )
    assert manager._persist_message(captured) is False

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Message)) == 0
        assert session.scalar(select(func.count()).select_from(Signal)) == 0
        assert session.scalar(select(func.count()).select_from(Position)) == 0
