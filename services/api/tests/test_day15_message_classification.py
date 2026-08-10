"""Day 15 acceptance checks for conservative message classification."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db import get_engine, get_session_factory
from app.message_classifier import classify_message
from app.models import Message, Position, Signal, Source, TelegramAccount
from app.seed import seed_owner
from app.telegram_crypto import TelegramSessionCipher
from app.telegram_listener import CapturedTelegramMessage
from app.telegram_listener_day13 import CapturedTelegramEdit
from app.telegram_listener_day15 import Day15TelegramListenerManager

DATABASE_URL = os.getenv("DATABASE_URL")
API_ROOT = Path(__file__).resolve().parents[1]
TEST_FERNET_KEY = "SY6ZSyA-C-HLcoOd_Wy60cnF3wVElxxhhDxwPVkrQtA="


def _alembic_config() -> Config:
    config = Config(str(API_ROOT / "alembic.ini"))
    if DATABASE_URL:
        config.set_main_option("sqlalchemy.url", DATABASE_URL)
    return config


@pytest.mark.parametrize(
    ("raw_text", "expected"),
    [
        (
            "BUY XAUUSD @ 3360\nSL 3350\nTP1 3370\nTP2 3380",
            "new_trade",
        ),
        ("Move SL to BE and secure profit", "trade_update"),
        ("TP1 HIT - move SL to break even", "trade_update"),
        ("Good morning family ❤️", "chatter"),
        (
            "First targets of the week are hit! First trade, first profits. Simple as that! ❤️",
            "chatter",
        ),
        ("Gold looks bullish today, watching the market", "chatter"),
        ("BUY GOLD", "uncertain"),
    ],
)
def test_known_examples_classify_conservatively(raw_text: str, expected: str) -> None:
    result = classify_message(raw_text)
    assert result.classification == expected


def test_reply_management_is_trade_update() -> None:
    result = classify_message("SL to BE please", reply_to_message_id=101)
    assert result.classification == "trade_update"
    assert result.decision_status == "classified"


def test_conflicting_trade_intent_is_held_for_review() -> None:
    result = classify_message(
        "BUY XAUUSD @ 3360 SL 3350 TP 3370 but SELL now",
    )
    assert result.classification == "uncertain"
    assert result.decision_status == "review"


@pytest.fixture()
def day15_environment(monkeypatch: pytest.MonkeyPatch):
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
            phone_number_e164="+359881234515",
            session_ciphertext=cipher.encrypt("day15-primary-session"),
            session_fingerprint=cipher.fingerprint("day15-primary-session"),
            status="connected",
        )
        session.add(reader)
        session.flush()
        source = Source(
            telegram_account_id=reader.id,
            chat_id=-10015001,
            chat_title="Day 15 Signals",
            source_alias="Day 15 Signals",
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
        ids = {"owner": owner.id, "reader": reader.id, "source": source.id}

    manager = Day15TelegramListenerManager(
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


def test_new_message_is_classified_without_creating_trade_objects(day15_environment) -> None:
    engine, manager, ids = day15_environment
    captured = CapturedTelegramMessage(
        source_id=ids["source"],
        chat_id=-10015001,
        telegram_message_id=15001,
        raw_text="BUY XAUUSD @ 3360\nSL 3350\nTP1 3370",
        posted_at=datetime.now(UTC),
        reply_to_message_id=None,
        has_media=False,
        media_type=None,
    )
    assert manager._persist_message(captured) is True

    with Session(engine) as session:
        row = session.execute(
            text(
                """
                SELECT classification, decision_status, revision_index
                FROM message_classifications
                """
            )
        ).mappings().one()
        assert row["classification"] == "new_trade"
        assert row["decision_status"] == "classified"
        assert row["revision_index"] == 0
        assert session.scalar(select(func.count()).select_from(Signal)) == 0
        assert session.scalar(select(func.count()).select_from(Position)) == 0


def test_edit_reclassifies_without_overwriting_original_evidence(day15_environment) -> None:
    engine, manager, ids = day15_environment
    original = CapturedTelegramMessage(
        source_id=ids["source"],
        chat_id=-10015001,
        telegram_message_id=15002,
        raw_text="BUY GOLD",
        posted_at=datetime.now(UTC),
        reply_to_message_id=None,
        has_media=False,
        media_type=None,
    )
    assert manager._persist_message(original) is True

    edited = CapturedTelegramEdit(
        source_id=ids["source"],
        chat_id=-10015001,
        telegram_message_id=15002,
        raw_text="BUY XAUUSD @ 3360 SL 3350 TP1 3370",
        edited_at=datetime.now(UTC),
        reply_to_message_id=None,
        has_media=False,
        media_type=None,
    )
    assert manager._persist_edit(edited) is True

    with Session(engine) as session:
        message = session.scalar(
            select(Message).where(Message.telegram_message_id == 15002)
        )
        assert message is not None
        assert message.raw_text == "BUY GOLD"
        rows = session.execute(
            text(
                """
                SELECT revision_index, classification, decision_status
                FROM message_classifications
                WHERE message_id = :message_id
                ORDER BY revision_index ASC
                """
            ),
            {"message_id": message.id},
        ).mappings().all()
        assert [(row["revision_index"], row["classification"]) for row in rows] == [
            (0, "uncertain"),
            (1, "new_trade"),
        ]
        assert rows[0]["decision_status"] == "review"
        assert rows[1]["decision_status"] == "classified"
        assert session.scalar(select(func.count()).select_from(Signal)) == 0
        assert session.scalar(select(func.count()).select_from(Position)) == 0
