"""Day 13 acceptance checks for edits, deletions and replies."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db import get_engine, get_session_factory
from app.models import AuditEvent, Message, Position, Signal, Source, TelegramAccount
from app.seed import seed_owner
from app.telegram_crypto import TelegramSessionCipher
from app.telegram_listener import CapturedTelegramMessage
from app.telegram_listener_day13 import CapturedTelegramEdit, Day13TelegramListenerManager

DATABASE_URL = os.getenv("DATABASE_URL")
API_ROOT = Path(__file__).resolve().parents[1]
TEST_FERNET_KEY = "SY6ZSyA-C-HLcoOd_Wy60cnF3wVElxxhhDxwPVkrQtA="


def _alembic_config() -> Config:
    config = Config(str(API_ROOT / "alembic.ini"))
    if DATABASE_URL:
        config.set_main_option("sqlalchemy.url", DATABASE_URL)
    return config


@pytest.fixture()
def day13_environment(monkeypatch: pytest.MonkeyPatch):
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
            label="Lifecycle reader",
            phone_number_e164="+359881234599",
            session_ciphertext=cipher.encrypt("day13-reader-session"),
            session_fingerprint=cipher.fingerprint("day13-reader-session"),
            status="connected",
        )
        session.add(reader)
        session.flush()
        source = Source(
            telegram_account_id=reader.id,
            chat_id=-10013001,
            chat_title="Lifecycle Signals",
            source_alias="Lifecycle Signals",
            status="testing",
            created_by_user_id=owner.id,
        )
        paused_source = Source(
            telegram_account_id=reader.id,
            chat_id=-10013002,
            chat_title="Paused Lifecycle",
            source_alias="Paused Lifecycle",
            status="paused",
            created_by_user_id=owner.id,
        )
        session.add_all([source, paused_source])
        session.flush()
        for item in (source, paused_source):
            session.execute(
                text(
                    """
                    INSERT INTO source_reader_access (
                        source_id, telegram_account_id, created_by_user_id
                    ) VALUES (:source_id, :reader_id, :owner_id)
                    """
                ),
                {"source_id": item.id, "reader_id": reader.id, "owner_id": owner.id},
            )
        session.commit()
        ids = {"source": source.id, "paused_source": paused_source.id}

    manager = Day13TelegramListenerManager(
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


def test_edit_appends_revision_preserves_original_and_audits(day13_environment) -> None:
    engine, manager, ids = day13_environment
    posted_at = datetime.now(UTC) - timedelta(minutes=1)
    original = CapturedTelegramMessage(
        source_id=ids["source"],
        chat_id=-10013001,
        telegram_message_id=13001,
        raw_text="BUY GOLD NOW",
        posted_at=posted_at,
        reply_to_message_id=12990,
        has_media=False,
        media_type=None,
    )
    assert manager._persist_message(original) is True

    edited_at = datetime.now(UTC)
    edit = CapturedTelegramEdit(
        source_id=ids["source"],
        chat_id=-10013001,
        telegram_message_id=13001,
        raw_text="BUY GOLD NOW SL 2370",
        edited_at=edited_at,
        reply_to_message_id=12990,
        has_media=False,
        media_type=None,
    )
    assert manager._persist_edit(edit) is True
    assert manager._persist_edit(edit) is False

    with Session(engine) as session:
        message = session.scalar(
            select(Message).where(
                Message.source_id == ids["source"],
                Message.telegram_message_id == 13001,
            )
        )
        assert message is not None
        assert message.raw_text == "BUY GOLD NOW"
        assert message.raw_payload["reply_to_message_id"] == 12990
        assert message.edited_at == edited_at

        revisions = session.execute(
            text(
                """
                SELECT revision_index, raw_text, raw_payload, edited_at
                FROM message_revisions
                WHERE message_id = :message_id
                ORDER BY revision_index ASC
                """
            ),
            {"message_id": message.id},
        ).mappings().all()
        assert len(revisions) == 1
        assert revisions[0]["revision_index"] == 1
        assert revisions[0]["raw_text"] == "BUY GOLD NOW SL 2370"
        assert revisions[0]["raw_payload"]["reply_to_message_id"] == 12990

        audit = session.scalar(
            select(AuditEvent).where(
                AuditEvent.event_type == "telegram.message_edited",
                AuditEvent.entity_id == message.id,
            )
        )
        assert audit is not None
        assert audit.payload["revision_index"] == 1
        assert audit.payload["original_preserved"] is True
        assert audit.payload["trade_action_created"] is False
        assert session.scalar(select(func.count()).select_from(Signal)) == 0
        assert session.scalar(select(func.count()).select_from(Position)) == 0


def test_deletion_marks_original_and_creates_no_trade_action(day13_environment) -> None:
    engine, manager, ids = day13_environment
    captured = CapturedTelegramMessage(
        source_id=ids["source"],
        chat_id=-10013001,
        telegram_message_id=13002,
        raw_text="DELETE ME LATER",
        posted_at=datetime.now(UTC),
        reply_to_message_id=None,
        has_media=False,
        media_type=None,
    )
    assert manager._persist_message(captured) is True
    deleted_at = datetime.now(UTC)
    assert manager._persist_deletion(
        ids["source"], -10013001, (13002,), deleted_at
    ) == 1
    # A replayed Telegram deletion event is harmless.
    assert manager._persist_deletion(
        ids["source"], -10013001, (13002,), deleted_at
    ) == 0

    with Session(engine) as session:
        message = session.scalar(
            select(Message).where(Message.telegram_message_id == 13002)
        )
        assert message is not None
        assert message.raw_text == "DELETE ME LATER"
        assert message.deleted_at == deleted_at
        deletion_events = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.event_type == "telegram.messages_deleted")
            .order_by(AuditEvent.id.asc())
        ).all()
        assert len(deletion_events) == 2
        assert deletion_events[0].payload["trade_action_created"] is False
        assert deletion_events[0].payload["newly_marked_deleted"] == 1
        assert deletion_events[1].payload["newly_marked_deleted"] == 0
        assert session.scalar(select(func.count()).select_from(Signal)) == 0
        assert session.scalar(select(func.count()).select_from(Position)) == 0


def test_chatless_deletion_resolves_only_unique_active_source(day13_environment) -> None:
    engine, manager, ids = day13_environment
    telegram_message_id = 13003
    active_message = CapturedTelegramMessage(
        source_id=ids["source"],
        chat_id=-10013001,
        telegram_message_id=telegram_message_id,
        raw_text="ACTIVE CHATLESS DELETE",
        posted_at=datetime.now(UTC),
        reply_to_message_id=None,
        has_media=False,
        media_type=None,
    )
    assert manager._persist_message(active_message) is True

    # Create the same Telegram-local message ID in another source, then pause it.
    with Session(engine) as session:
        paused_source = session.get(Source, ids["paused_source"])
        assert paused_source is not None
        paused_source.status = "testing"
        session.commit()
    colliding_message = CapturedTelegramMessage(
        source_id=ids["paused_source"],
        chat_id=-10013002,
        telegram_message_id=telegram_message_id,
        raw_text="COLLIDING CHATLESS DELETE",
        posted_at=datetime.now(UTC),
        reply_to_message_id=None,
        has_media=False,
        media_type=None,
    )
    assert manager._persist_message(colliding_message) is True

    # Two active selected sources with the same chat-local ID are ambiguous.
    assert manager._resolve_chatless_deletions(
        (ids["source"], ids["paused_source"]),
        (telegram_message_id,),
    ) == ()

    with Session(engine) as session:
        paused_source = session.get(Source, ids["paused_source"])
        assert paused_source is not None
        paused_source.status = "paused"
        session.commit()

    # Once the collision is paused, the active source is uniquely resolvable.
    targets = manager._resolve_chatless_deletions(
        (ids["source"], ids["paused_source"]),
        (telegram_message_id,),
    )
    assert targets == ((ids["source"], -10013001, (telegram_message_id,)),)
    deleted_at = datetime.now(UTC)
    source_id, chat_id, resolved_ids = targets[0]
    assert manager._persist_deletion(source_id, chat_id, resolved_ids, deleted_at) == 1

    with Session(engine) as session:
        active = session.scalar(
            select(Message).where(
                Message.source_id == ids["source"],
                Message.telegram_message_id == telegram_message_id,
            )
        )
        paused = session.scalar(
            select(Message).where(
                Message.source_id == ids["paused_source"],
                Message.telegram_message_id == telegram_message_id,
            )
        )
        assert active is not None and active.deleted_at == deleted_at
        assert paused is not None and paused.deleted_at is None
        assert session.scalar(select(func.count()).select_from(Signal)) == 0
        assert session.scalar(select(func.count()).select_from(Position)) == 0


def test_edit_for_paused_source_is_ignored_and_missing_original_is_audited(day13_environment) -> None:
    engine, manager, ids = day13_environment
    now = datetime.now(UTC)

    paused_edit = CapturedTelegramEdit(
        source_id=ids["paused_source"],
        chat_id=-10013002,
        telegram_message_id=13100,
        raw_text="PAUSED EDIT",
        edited_at=now,
        reply_to_message_id=None,
        has_media=False,
        media_type=None,
    )
    assert manager._persist_edit(paused_edit) is False

    missing_original = CapturedTelegramEdit(
        source_id=ids["source"],
        chat_id=-10013001,
        telegram_message_id=13101,
        raw_text="EDIT WITHOUT ORIGINAL",
        edited_at=now,
        reply_to_message_id=13099,
        has_media=False,
        media_type=None,
    )
    assert manager._persist_edit(missing_original) is False

    with Session(engine) as session:
        missing_audits = session.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type == "telegram.message_edit_missing_original"
            )
        ).all()
        assert len(missing_audits) == 1
        assert missing_audits[0].entity_id == ids["source"]
        assert missing_audits[0].payload["telegram_message_id"] == 13101
        assert missing_audits[0].payload["trade_action_created"] is False
        assert session.scalar(select(func.count()).select_from(Message)) == 0
        assert session.scalar(select(func.count()).select_from(Signal)) == 0
        assert session.scalar(select(func.count()).select_from(Position)) == 0


def test_repeated_edits_of_the_same_orphan_are_logged_once_per_hour(day13_environment) -> None:
    """A provider that edits one pinned, never-captured message forever must not flood
    the audit trail. Production saw the same handful of messages re-delivered roughly
    every 90 seconds, continuously, for hours: 3,400+ identical rows, one DB write each,
    for a finding that never changed. One row per message per hour is enough."""
    engine, manager, ids = day13_environment
    now = datetime.now(UTC)

    def orphan_edit(message_id: int) -> CapturedTelegramEdit:
        return CapturedTelegramEdit(
            source_id=ids["source"],
            chat_id=-10013001,
            telegram_message_id=message_id,
            raw_text="PINNED STATUS EDIT",
            edited_at=now,
            reply_to_message_id=None,
            has_media=False,
            media_type=None,
        )

    assert manager._persist_edit(orphan_edit(13200)) is False
    # Re-delivered several times in quick succession, as Telegram actually did in
    # production. Still an orphan every time, so still no trade action either way.
    assert manager._persist_edit(orphan_edit(13200)) is False
    assert manager._persist_edit(orphan_edit(13200)) is False

    with Session(engine) as session:
        rows = session.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type == "telegram.message_edit_missing_original",
                AuditEvent.payload["telegram_message_id"].astext == "13200",
            )
        ).all()
        assert len(rows) == 1, "three re-deliveries within the hour must log once, not three times"

        # A different orphaned message is unaffected by another message's cooldown.
        session.execute(text("SELECT 1"))  # keep the session warm for the next assert

    assert manager._persist_edit(orphan_edit(13201)) is False
    with Session(engine) as session:
        other = session.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type == "telegram.message_edit_missing_original",
                AuditEvent.payload["telegram_message_id"].astext == "13201",
            )
        ).all()
        assert len(other) == 1

        # Once the cooldown has genuinely passed, the finding is logged again - this
        # stays evidence of an ongoing condition, not a one-time note that goes stale.
        # audit_events is append-only (no UPDATE/DELETE), so this proves it by seeding
        # an hour-old occurrence directly, exactly as a real one would read by the time
        # the cooldown expires, for a message never seen in this test before.
        session.execute(
            text(
                """
                INSERT INTO audit_events (event_type, entity_type, entity_id, payload, created_at)
                VALUES (
                    'telegram.message_edit_missing_original', 'source', :source_id,
                    CAST(:payload AS jsonb), now() - INTERVAL '2 hours'
                )
                """
            ),
            {
                "source_id": ids["source"],
                "payload": '{"telegram_message_id": 13300, "chat_id": -10013001}',
            },
        )
        session.commit()

    assert manager._persist_edit(orphan_edit(13300)) is False
    with Session(engine) as session:
        rows = session.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type == "telegram.message_edit_missing_original",
                AuditEvent.payload["telegram_message_id"].astext == "13300",
            )
        ).all()
        assert len(rows) == 2, "past the cooldown, the condition is logged again"
