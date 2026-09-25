"""Regression tests for the Sep-24 "pre-day settlement replay" hotfix.

That hotfix (commit 5de252ff) fixed a real one-time incident (a backfill replayed old,
already-known settlements as if they were new) but did it by comparing every broker
settlement's occurred_at against "today" (recomputed on every run). That meant every
night, the instant the Sofia calendar day rolled over, the exact same logic:

  - deleted already-SENT settlement messages whose event happened earlier that same day
    (perpetually, not just for the original incident), and
  - blocked PENDING settlement messages from ever being sent once their event fell
    before "today's" boundary, even though nothing was wrong with them.

Night-hours providers (TIG's Asia Trades in particular) trade right around the
boundary, so this reliably ate real stop-loss/trade-complete notifications every day.

These tests exercise the real SQL against a real Postgres database, not just the
Python-level conditionals, since the bug lived entirely in raw SQL WHERE clauses.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db import get_engine, get_session_factory
from app.security import hash_password
from app.seed import seed_owner
from app.telegram_crypto import TelegramSessionCipher
from app.telegram_publisher_canonical import CanonicalTelegramPublisherManager
from app.telegram_trade_ledger import TelegramTradeLedger

DATABASE_URL = os.getenv("DATABASE_URL")
API_ROOT = Path(__file__).resolve().parents[1]
TEST_FERNET_KEY = "SY6ZSyA-C-HLcoOd_Wy60cnF3wVElxxhhDxwPVkrQtA="


def _alembic_config() -> Config:
    config = Config(str(API_ROOT / "alembic.ini"))
    if DATABASE_URL:
        config.set_main_option("sqlalchemy.url", DATABASE_URL)
    return config


@pytest.fixture()
def db(monkeypatch: pytest.MonkeyPatch):
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")

    monkeypatch.setenv("SUPER_SIGNALS_ENV", "test")
    monkeypatch.setenv("SUPER_SIGNALS_COOKIE_SECURE", "false")
    monkeypatch.setenv("SUPER_SIGNALS_FINGERPRINT_SECRET", "test-fingerprint-secret")
    monkeypatch.setenv("SUPER_SIGNALS_TELEGRAM_SESSION_KEYS", TEST_FERNET_KEY)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    config = _alembic_config()
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    engine = create_engine(DATABASE_URL, future=True)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    cipher = TelegramSessionCipher((TEST_FERNET_KEY,))

    with Session(engine) as session:
        owner = seed_owner(session, "owner@example.com", "Danny")
        session.execute(
            text("UPDATE users SET password_hash = :hash WHERE id = :id"),
            {"hash": hash_password("owner password 123"), "id": owner.id},
        )
        reader_id = session.execute(
            text(
                """
                INSERT INTO telegram_accounts (
                    id, owner_user_id, label, phone_number_e164,
                    session_ciphertext, session_fingerprint, status
                ) VALUES (
                    :id, :owner_id, 'Owner reader', '+359881234567',
                    :ciphertext, :fingerprint, 'connected'
                ) RETURNING id
                """
            ),
            {
                "id": uuid4(),
                "owner_id": owner.id,
                "ciphertext": cipher.encrypt("day-boundary-reader"),
                "fingerprint": cipher.fingerprint("day-boundary-reader"),
            },
        ).scalar_one()
        source_id = session.execute(
            text(
                """
                INSERT INTO sources (
                    id, telegram_account_id, chat_id, chat_title, source_alias,
                    status, created_by_user_id
                ) VALUES (
                    :id, :reader_id, -1009999, 'TIG''s Asia Trades', 'TIG''s Asia Trades',
                    'live', :owner_id
                ) RETURNING id
                """
            ),
            {"id": uuid4(), "reader_id": reader_id, "owner_id": owner.id},
        ).scalar_one()
        session.commit()
        owner_id = owner.id

    yield session_factory, owner_id, source_id

    command.downgrade(config, "base")
    engine.dispose()
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


def _make_message(session: Session, source_id, telegram_message_id: int, posted_at: datetime):
    return session.execute(
        text(
            """
            INSERT INTO messages (
                id, source_id, telegram_message_id, raw_text, posted_at, content_sha256
            ) VALUES (
                :id, :source_id, :tg_id, 'irrelevant for this test', :posted_at, :sha
            )
            RETURNING id
            """
        ),
        {
            "id": uuid4(),
            "source_id": source_id,
            "tg_id": telegram_message_id,
            "posted_at": posted_at,
            "sha": uuid4().hex,
        },
    ).scalar_one()


def _make_signal(session: Session, source_id, message_id, provider_message_id: int):
    return session.execute(
        text(
            """
            INSERT INTO signals (
                id, source_message_id, symbol, side, source_id, provider_chat_id,
                provider_message_id, source_posted_at, signal_fingerprint,
                original_text, member_trade_number
            ) VALUES (
                :id, :message_id, 'XAUUSD', 'BUY', :source_id, -1009999,
                :provider_message_id, now(), :fingerprint,
                'BUY XAUUSD', :trade_number
            )
            RETURNING id
            """
        ),
        {
            "id": uuid4(),
            "message_id": message_id,
            "source_id": source_id,
            "provider_message_id": provider_message_id,
            "fingerprint": uuid4().hex,
            "trade_number": provider_message_id % 30000 + 1,
        },
    ).scalar_one()


def _make_position(
    session: Session,
    *,
    signal_id,
    user_id,
    tp_index: int,
    closed_at: datetime | None,
    pnl_amount,
):
    return session.execute(
        text(
            """
            INSERT INTO positions (
                id, signal_id, user_id, tp_index, planned_risk_percent, status,
                closed_at, pnl_amount, opened_at, entry_index, entry_order_type
            ) VALUES (
                :id, :signal_id, :user_id, :tp_index, 1, 'closed',
                :closed_at, :pnl_amount, :closed_at, 1, 'market'
            )
            RETURNING id
            """
        ),
        {
            "id": uuid4(),
            "signal_id": signal_id,
            "user_id": user_id,
            "tp_index": tp_index,
            "closed_at": closed_at,
            "pnl_amount": pnl_amount,
        },
    ).scalar_one()


def _make_outcome(
    session: Session, *, position_id, user_id, signal_id, status: str, cash_pnl, closed_at
):
    session.execute(
        text(
            """
            INSERT INTO performance_trade_outcomes (
                position_id, user_id, signal_id, symbol, side, status, cash_pnl,
                closed_at, broker_deal_count, planned_risk_percent, source_digest
            ) VALUES (
                :position_id, :user_id, :signal_id, 'XAUUSD', 'BUY', :status,
                :cash_pnl, :closed_at, 1, 1, :source_digest
            )
            """
        ),
        {
            "position_id": position_id,
            "user_id": user_id,
            "signal_id": signal_id,
            "status": status,
            "cash_pnl": cash_pnl,
            "closed_at": closed_at,
            "source_digest": uuid4().hex,
        },
    )


def _make_settlement_event(
    session: Session,
    *,
    signal_id,
    message_id,
    position_id,
    occurred_at: datetime,
    rendered_text: str = "TP1 position closed",
):
    return session.execute(
        text(
            """
            INSERT INTO signal_lifecycle_events (
                id, signal_id, source_message_id, event_type, event_key, origin,
                rendered_text, aggregate_result, occurred_at
            ) VALUES (
                :id, :signal_id, :message_id, 'broker_position_settled', :event_key,
                'broker', :rendered_text, CAST(:aggregate_result AS jsonb), :occurred_at
            )
            RETURNING id
            """
        ),
        {
            "id": uuid4(),
            "signal_id": signal_id,
            "message_id": message_id,
            "event_key": f"broker-position-settled:{position_id}",
            "rendered_text": rendered_text,
            "aggregate_result": f'{{"position_id": "{position_id}"}}',
            "occurred_at": occurred_at,
        },
    ).scalar_one()


def _make_publication(
    session: Session,
    *,
    signal_id,
    lifecycle_event_id=None,
    status: str = "pending",
    sent_at: datetime | None = None,
    telegram_message_id: int | None = None,
):
    return session.execute(
        text(
            """
            INSERT INTO telegram_publications (
                id, signal_id, publication_kind, lifecycle_event_id, status,
                sent_at, telegram_message_id
            ) VALUES (
                :id, :signal_id, 'lifecycle_event', :lifecycle_event_id, :status,
                :sent_at, :telegram_message_id
            )
            RETURNING id
            """
        ),
        {
            "id": uuid4(),
            "signal_id": signal_id,
            "lifecycle_event_id": lifecycle_event_id,
            "status": status,
            "sent_at": sent_at,
            "telegram_message_id": telegram_message_id,
        },
    ).scalar_one()


def _manager(session_factory, owner_id) -> CanonicalTelegramPublisherManager:
    manager = object.__new__(CanonicalTelegramPublisherManager)
    manager._session_factory = session_factory
    manager._destination_chat_id = -1009999
    manager._bot_token = "test-token"
    manager._reference_user_id = owner_id
    manager._trade_ledger = TelegramTradeLedger(session_factory, owner_id)
    return manager


def test_settlement_replay_cleanup_only_touches_the_original_incident_window(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_factory, owner_id, source_id = db
    deleted_message_ids: list[int] = []
    monkeypatch.setattr(
        "app.telegram_publisher_canonical._bot_api_call",
        lambda token, method, payload: (
            deleted_message_ids.append(payload["message_id"]) or {"ok": True}
        ),
    )

    with session_factory() as session:
        # Incident row: an old settlement (weeks stale relative to the incident) that
        # was actually sent inside the original Sep-24 14:54-18:00 UTC replay window.
        old_message = _make_message(session, source_id, 1, datetime(2026, 8, 20, tzinfo=UTC))
        old_signal = _make_signal(session, source_id, old_message, 1)
        old_position = _make_position(
            session,
            signal_id=old_signal,
            user_id=owner_id,
            tp_index=1,
            closed_at=datetime(2026, 8, 20, 10, tzinfo=UTC),
            pnl_amount="5.00",
        )
        old_event = _make_settlement_event(
            session,
            signal_id=old_signal,
            message_id=old_message,
            position_id=old_position,
            occurred_at=datetime(2026, 8, 20, 10, tzinfo=UTC),
        )
        incident_publication = _make_publication(
            session,
            signal_id=old_signal,
            lifecycle_event_id=old_event,
            status="sent",
            sent_at=datetime(2026, 9, 24, 15, 5, tzinfo=UTC),
            telegram_message_id=555,
        )

        # Present-day row: a settlement from more than a day ago (so it would look
        # "historical" under a moving day-boundary comparison) but sent recently, long
        # after the incident window. This must survive.
        fresh_message = _make_message(session, source_id, 2, datetime.now(UTC))
        fresh_signal = _make_signal(session, source_id, fresh_message, 2)
        fresh_position = _make_position(
            session,
            signal_id=fresh_signal,
            user_id=owner_id,
            tp_index=1,
            closed_at=datetime.now(UTC) - timedelta(hours=30),
            pnl_amount="-16.75",
        )
        fresh_event = _make_settlement_event(
            session,
            signal_id=fresh_signal,
            message_id=fresh_message,
            position_id=fresh_position,
            occurred_at=datetime.now(UTC) - timedelta(hours=30),
        )
        present_day_publication = _make_publication(
            session,
            signal_id=fresh_signal,
            lifecycle_event_id=fresh_event,
            status="sent",
            sent_at=datetime.now(UTC) - timedelta(hours=1),
            telegram_message_id=777,
        )
        session.commit()

    manager = _manager(session_factory, owner_id)
    manager._cleanup_accidental_historical_settlement_replay_safely()

    with session_factory() as session:
        incident_row = session.execute(
            text("SELECT status, failure_code FROM telegram_publications WHERE id=:id"),
            {"id": incident_publication},
        ).mappings().one()
        present_day_row = session.execute(
            text("SELECT status, failure_code FROM telegram_publications WHERE id=:id"),
            {"id": present_day_publication},
        ).mappings().one()

    assert incident_row["status"] == "suppressed"
    assert incident_row["failure_code"] == "historical_replay_deleted"
    assert deleted_message_ids == [555]

    # The real, same-day settlement message must never be swept up just because the
    # calendar day has since rolled over.
    assert present_day_row["status"] == "sent"
    assert present_day_row["failure_code"] is None


def test_late_arriving_settlement_is_claimed_despite_crossing_the_day_boundary(db) -> None:
    session_factory, owner_id, source_id = db
    with session_factory() as session:
        message = _make_message(session, source_id, 10, datetime.now(UTC))
        signal_id = _make_signal(session, source_id, message, 10)
        occurred_at = datetime.now(UTC) - timedelta(hours=26)
        position_id = _make_position(
            session,
            signal_id=signal_id,
            user_id=owner_id,
            tp_index=1,
            closed_at=occurred_at,
            pnl_amount="-16.75",
        )
        _make_outcome(
            session,
            position_id=position_id,
            user_id=owner_id,
            signal_id=signal_id,
            status="lost",
            cash_pnl="-16.75",
            closed_at=occurred_at,
        )
        event_id = _make_settlement_event(
            session,
            signal_id=signal_id,
            message_id=message,
            position_id=position_id,
            occurred_at=occurred_at,
        )
        publication_id = _make_publication(
            session, signal_id=signal_id, lifecycle_event_id=event_id
        )
        session.commit()

    manager = _manager(session_factory, owner_id)
    attempt = manager._claim_lifecycle()

    assert attempt is not None
    assert attempt.publication_id == publication_id

    with session_factory() as session:
        row = session.execute(
            text("SELECT status, attempt_count FROM telegram_publications WHERE id=:id"),
            {"id": publication_id},
        ).mappings().one()
    assert row["status"] == "sending"
    assert row["attempt_count"] == 1


def test_stuck_unsettled_earlier_leg_stops_blocking_siblings_after_a_grace_period(db) -> None:
    session_factory, owner_id, source_id = db
    with session_factory() as session:
        message = _make_message(session, source_id, 20, datetime.now(UTC))
        signal_id = _make_signal(session, source_id, message, 20)

        # Leg A closed a long time ago but never got its own settlement event, exactly
        # like the real TIG legs whose performance_trade_outcomes row stayed 'open'
        # after the position itself closed. It must not block its sibling forever.
        _make_position(
            session,
            signal_id=signal_id,
            user_id=owner_id,
            tp_index=1,
            closed_at=datetime.now(UTC) - timedelta(hours=3),
            pnl_amount=None,
        )

        # Leg B is the real, fully-settled close whose Telegram message is stuck.
        leg_b_closed_at = datetime.now(UTC) - timedelta(minutes=10)
        leg_b_position = _make_position(
            session,
            signal_id=signal_id,
            user_id=owner_id,
            tp_index=2,
            closed_at=leg_b_closed_at,
            pnl_amount="16.75",
        )
        _make_outcome(
            session,
            position_id=leg_b_position,
            user_id=owner_id,
            signal_id=signal_id,
            status="won",
            cash_pnl="16.75",
            closed_at=leg_b_closed_at,
        )
        leg_b_event = _make_settlement_event(
            session,
            signal_id=signal_id,
            message_id=message,
            position_id=leg_b_position,
            occurred_at=leg_b_closed_at,
        )
        publication_id = _make_publication(
            session, signal_id=signal_id, lifecycle_event_id=leg_b_event
        )
        session.commit()

    manager = _manager(session_factory, owner_id)
    attempt = manager._claim_lifecycle()

    assert attempt is not None
    assert attempt.publication_id == publication_id


def test_recently_unsettled_earlier_leg_still_blocks_its_sibling(db) -> None:
    session_factory, owner_id, source_id = db
    with session_factory() as session:
        message = _make_message(session, source_id, 21, datetime.now(UTC))
        signal_id = _make_signal(session, source_id, message, 21)

        # Leg A closed moments ago and has not had time to settle yet - the ordering
        # guard should still hold leg B back so trades are announced in order.
        _make_position(
            session,
            signal_id=signal_id,
            user_id=owner_id,
            tp_index=1,
            closed_at=datetime.now(UTC) - timedelta(minutes=2),
            pnl_amount=None,
        )

        leg_b_closed_at = datetime.now(UTC) - timedelta(minutes=1)
        leg_b_position = _make_position(
            session,
            signal_id=signal_id,
            user_id=owner_id,
            tp_index=2,
            closed_at=leg_b_closed_at,
            pnl_amount="16.75",
        )
        _make_outcome(
            session,
            position_id=leg_b_position,
            user_id=owner_id,
            signal_id=signal_id,
            status="won",
            cash_pnl="16.75",
            closed_at=leg_b_closed_at,
        )
        leg_b_event = _make_settlement_event(
            session,
            signal_id=signal_id,
            message_id=message,
            position_id=leg_b_position,
            occurred_at=leg_b_closed_at,
        )
        _make_publication(session, signal_id=signal_id, lifecycle_event_id=leg_b_event)
        session.commit()

    manager = _manager(session_factory, owner_id)
    attempt = manager._claim_lifecycle()

    assert attempt is None


def test_final_settlement_says_stop_loss_hit_when_the_closing_leg_is_the_stop_loss(db) -> None:
    session_factory, owner_id, source_id = db
    with session_factory() as session:
        message = _make_message(session, source_id, 30, datetime.now(UTC))
        signal_id = _make_signal(session, source_id, message, 30)

        leg1_closed_at = datetime.now(UTC) - timedelta(minutes=20)
        leg1_position = _make_position(
            session,
            signal_id=signal_id,
            user_id=owner_id,
            tp_index=1,
            closed_at=leg1_closed_at,
            pnl_amount="10.00",
        )
        _make_outcome(
            session,
            position_id=leg1_position,
            user_id=owner_id,
            signal_id=signal_id,
            status="won",
            cash_pnl="10.00",
            closed_at=leg1_closed_at,
        )
        _make_settlement_event(
            session,
            signal_id=signal_id,
            message_id=message,
            position_id=leg1_position,
            occurred_at=leg1_closed_at,
        )

        leg2_closed_at = datetime.now(UTC) - timedelta(minutes=1)
        leg2_position = _make_position(
            session,
            signal_id=signal_id,
            user_id=owner_id,
            tp_index=2,
            closed_at=leg2_closed_at,
            pnl_amount="-25.00",
        )
        _make_outcome(
            session,
            position_id=leg2_position,
            user_id=owner_id,
            signal_id=signal_id,
            status="lost",
            cash_pnl="-25.00",
            closed_at=leg2_closed_at,
        )
        leg2_event = _make_settlement_event(
            session,
            signal_id=signal_id,
            message_id=message,
            position_id=leg2_position,
            occurred_at=leg2_closed_at,
        )
        publication_id = _make_publication(
            session, signal_id=signal_id, lifecycle_event_id=leg2_event
        )
        session.commit()

    manager = _manager(session_factory, owner_id)
    attempt = manager._claim_lifecycle()

    assert attempt is not None
    assert attempt.publication_id == publication_id
    assert "STOP LOSS HIT" in attempt.text
    assert "TRADE COMPLETE" in attempt.text
    assert "TRADE LOSS" in attempt.text


def test_final_settlement_stays_generic_when_the_closing_leg_itself_was_not_the_loss(db) -> None:
    session_factory, owner_id, source_id = db
    with session_factory() as session:
        message = _make_message(session, source_id, 31, datetime.now(UTC))
        signal_id = _make_signal(session, source_id, message, 31)

        # Leg 1 is the one that lost - dragging the trade total negative - but it is
        # NOT the closing leg, so the closing leg's own message must not claim it was
        # the stop loss.
        leg1_closed_at = datetime.now(UTC) - timedelta(minutes=20)
        leg1_position = _make_position(
            session,
            signal_id=signal_id,
            user_id=owner_id,
            tp_index=1,
            closed_at=leg1_closed_at,
            pnl_amount="-30.00",
        )
        _make_outcome(
            session,
            position_id=leg1_position,
            user_id=owner_id,
            signal_id=signal_id,
            status="lost",
            cash_pnl="-30.00",
            closed_at=leg1_closed_at,
        )
        _make_settlement_event(
            session,
            signal_id=signal_id,
            message_id=message,
            position_id=leg1_position,
            occurred_at=leg1_closed_at,
        )

        leg2_closed_at = datetime.now(UTC) - timedelta(minutes=1)
        leg2_position = _make_position(
            session,
            signal_id=signal_id,
            user_id=owner_id,
            tp_index=2,
            closed_at=leg2_closed_at,
            pnl_amount="5.00",
        )
        _make_outcome(
            session,
            position_id=leg2_position,
            user_id=owner_id,
            signal_id=signal_id,
            status="won",
            cash_pnl="5.00",
            closed_at=leg2_closed_at,
        )
        leg2_event = _make_settlement_event(
            session,
            signal_id=signal_id,
            message_id=message,
            position_id=leg2_position,
            occurred_at=leg2_closed_at,
        )
        publication_id = _make_publication(
            session, signal_id=signal_id, lifecycle_event_id=leg2_event
        )
        session.commit()

    manager = _manager(session_factory, owner_id)
    attempt = manager._claim_lifecycle()

    assert attempt is not None
    assert attempt.publication_id == publication_id
    assert "STOP LOSS HIT" not in attempt.text
    assert "TRADE COMPLETE" in attempt.text
    # -30 (leg 1, the real stop loss) + 5 (leg 2, this closing leg's own win) nets to
    # an overall trade loss, but this leg's own outcome was "won" - the heading must
    # reflect the earlier leg's loss generically, never claim this leg was the SL hit.
    assert "TRADE LOSS" in attempt.text
