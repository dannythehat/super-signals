"""Real-Postgres proof that broker_client_id resolution actually settles the position.

`test_broker_fill_settlement.py` proves the Python-level control flow with a stub
session; it cannot validate that the LATERAL join's SQL itself is correct against real
Postgres semantics. This file seeds the exact production shape found on the Owner
account - eleven positions marked 'error' with broker_position_id NULL, whose only link
back to their real broker fill is broker_client_id - and runs BrokerFillSettlementService
against a real database.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.broker_fill_settlement import BrokerFillSettlementService
from app.config import get_settings
from app.db import get_engine, get_session_factory
from app.seed import seed_owner

DATABASE_URL = os.getenv("DATABASE_URL")
API_ROOT = Path(__file__).resolve().parents[1]


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
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    config = _alembic_config()
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    engine = create_engine(DATABASE_URL, future=True)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    with Session(engine) as session:
        owner = seed_owner(session, "owner@example.com", "Danny")
        mt5_account_id = session.execute(
            text(
                """
                INSERT INTO mt5_accounts (
                    id, owner_user_id, account_environment, login, server,
                    metaapi_account_id, metaapi_token_ciphertext, metaapi_token_fingerprint,
                    status
                ) VALUES (
                    :id, :owner_id, 'live', '12345', 'Vantage-Live',
                    'meta-acct-1', '\\x00'::bytea, 'fingerprint', 'connected'
                )
                RETURNING id
                """
            ),
            {"id": uuid4(), "owner_id": owner.id},
        ).scalar_one()
        reader_id = session.execute(
            text(
                """
                INSERT INTO telegram_accounts (
                    id, owner_user_id, label, phone_number_e164,
                    session_ciphertext, session_fingerprint, status
                ) VALUES (
                    :id, :owner_id, 'Owner reader', '+359881234567',
                    '\\x00'::bytea, 'fingerprint', 'connected'
                ) RETURNING id
                """
            ),
            {"id": uuid4(), "owner_id": owner.id},
        ).scalar_one()
        source_id = session.execute(
            text(
                """
                INSERT INTO sources (
                    id, telegram_account_id, chat_id, chat_title, source_alias,
                    status, created_by_user_id
                ) VALUES (
                    :id, :reader_id, -100999, 'Test Source', 'Test Source',
                    'live', :owner_id
                ) RETURNING id
                """
            ),
            {"id": uuid4(), "reader_id": reader_id, "owner_id": owner.id},
        ).scalar_one()
        session.commit()
        owner_id = owner.id

    yield session_factory, owner_id, mt5_account_id, source_id

    command.downgrade(config, "base")
    engine.dispose()
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


def _make_signal(session: Session, *, source_id, provider_message_id: int):
    message_id = session.execute(
        text(
            """
            INSERT INTO messages (
                id, source_id, telegram_message_id, raw_text, posted_at, content_sha256
            ) VALUES (
                :id, :source_id, :tg_id, 'irrelevant for this test', now(), :sha
            )
            RETURNING id
            """
        ),
        {"id": uuid4(), "source_id": source_id, "tg_id": provider_message_id, "sha": uuid4().hex},
    ).scalar_one()
    return session.execute(
        text(
            """
            INSERT INTO signals (
                id, source_message_id, symbol, side, source_id, provider_chat_id,
                provider_message_id, source_posted_at, signal_fingerprint, original_text
            ) VALUES (
                :id, :message_id, 'XAUUSD', 'BUY', :source_id, -100999,
                :provider_message_id, now(), :fingerprint, 'BUY XAUUSD'
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
        },
    ).scalar_one()


def _make_stranded_position(session: Session, *, user_id, signal_id, broker_client_id: str):
    return session.execute(
        text(
            """
            INSERT INTO positions (
                id, signal_id, user_id, tp_index, planned_risk_percent, status,
                broker_client_id, close_reason, entry_index, entry_order_type
            ) VALUES (
                :id, :signal_id, :user_id, 1, 1, 'error',
                :broker_client_id, 'broker_filled_position_not_visible', 1, 'market'
            )
            RETURNING id
            """
        ),
        {
            "id": uuid4(),
            "signal_id": signal_id,
            "user_id": user_id,
            "broker_client_id": broker_client_id,
        },
    ).scalar_one()


def _make_deal(
    session: Session,
    *,
    user_id,
    mt5_account_id,
    broker_client_id: str,
    broker_position_id: str,
    entry_type: str,
    occurred_at: datetime,
    volume: str,
    price: str,
    profit: str = "0",
):
    session.execute(
        text(
            """
            INSERT INTO broker_deals (
                id, user_id, mt5_account_id, broker_deal_id, broker_position_id,
                broker_client_id, deal_type, entry_type, symbol, volume, price,
                profit, commission, swap, occurred_at, raw_payload
            ) VALUES (
                :id, :user_id, :mt5_account_id, :broker_deal_id, :broker_position_id,
                :broker_client_id, 'DEAL_TYPE_BUY', :entry_type, 'XAUUSD', :volume, :price,
                :profit, 0, 0, :occurred_at, '{}'::jsonb
            )
            """
        ),
        {
            "id": uuid4(),
            "user_id": user_id,
            "mt5_account_id": mt5_account_id,
            "broker_deal_id": uuid4().hex,
            "broker_position_id": broker_position_id,
            "broker_client_id": broker_client_id,
            "entry_type": entry_type,
            "volume": volume,
            "price": price,
            "profit": profit,
            "occurred_at": occurred_at,
        },
    )


def test_position_missing_broker_position_id_settles_closed_via_client_id(db) -> None:
    """The real shape of SS_842d397c7347_1: filled, then closed by the broker at a real
    profit, but the local row never received a broker_position_id and stayed 'error'."""
    session_factory, owner_id, mt5_account_id, source_id = db
    broker_client_id = "SS_842d397c7347_1"

    with session_factory() as session:
        signal_id = _make_signal(session, source_id=source_id, provider_message_id=1)
        position_id = _make_stranded_position(
            session, user_id=owner_id, signal_id=signal_id, broker_client_id=broker_client_id
        )
        _make_deal(
            session,
            user_id=owner_id,
            mt5_account_id=mt5_account_id,
            broker_client_id=broker_client_id,
            broker_position_id="1798655744",
            entry_type="DEAL_ENTRY_IN",
            occurred_at=datetime(2026, 8, 19, 12, 39, 34, tzinfo=UTC),
            volume="0.01",
            price="3510.00",
        )
        _make_deal(
            session,
            user_id=owner_id,
            mt5_account_id=mt5_account_id,
            broker_client_id=broker_client_id,
            broker_position_id="1798655744",
            entry_type="DEAL_ENTRY_OUT",
            occurred_at=datetime(2026, 8, 19, 12, 43, 11, tzinfo=UTC),
            volume="0.01",
            price="3514.84",
            profit="4.84",
        )
        session.commit()

    service = BrokerFillSettlementService(session_factory)
    result = service.settle_once()

    assert result.considered == 1
    assert result.settled_closed == 1

    with session_factory() as session:
        row = session.execute(
            text(
                "SELECT status, broker_position_id, pnl_amount, close_reason "
                "FROM positions WHERE id=:id"
            ),
            {"id": position_id},
        ).mappings().one()
    assert row["status"] == "closed"
    assert row["broker_position_id"] == "1798655744"
    assert row["pnl_amount"] == Decimal("4.84")
    assert row["close_reason"] == "broker_settled_closed"


def test_position_missing_broker_position_id_still_open_is_adopted_open(db) -> None:
    """Same missing-broker_position_id shape, but the broker never closed it: it must
    return to 'open' (so owner_manual_close.py's status='open' selection can reach it),
    not 'closed'."""
    session_factory, owner_id, mt5_account_id, source_id = db
    broker_client_id = "SS_f3abd98151e0_1"

    with session_factory() as session:
        signal_id = _make_signal(session, source_id=source_id, provider_message_id=2)
        position_id = _make_stranded_position(
            session, user_id=owner_id, signal_id=signal_id, broker_client_id=broker_client_id
        )
        _make_deal(
            session,
            user_id=owner_id,
            mt5_account_id=mt5_account_id,
            broker_client_id=broker_client_id,
            broker_position_id="2071240445",
            entry_type="DEAL_ENTRY_IN",
            occurred_at=datetime(2026, 9, 22, 15, 9, 58, tzinfo=UTC),
            volume="0.01",
            price="4338.12",
        )
        session.commit()

    service = BrokerFillSettlementService(session_factory)
    result = service.settle_once()

    assert result.adopted_open == 1

    with session_factory() as session:
        row = session.execute(
            text("SELECT status, broker_position_id, close_reason FROM positions WHERE id=:id"),
            {"id": position_id},
        ).mappings().one()
    assert row["status"] == "open"
    assert row["broker_position_id"] == "2071240445"
    assert row["close_reason"] is None


def test_known_broker_position_id_path_is_unaffected(db) -> None:
    """Regression guard: a position that already has broker_position_id set (the
    original, long-standing settlement path) must keep matching by that id and must not
    be disturbed by the new broker_client_id fallback."""
    session_factory, owner_id, mt5_account_id, source_id = db

    with session_factory() as session:
        signal_id = _make_signal(session, source_id=source_id, provider_message_id=3)
        position_id = session.execute(
            text(
                """
                INSERT INTO positions (
                    id, signal_id, user_id, tp_index, planned_risk_percent, status,
                    broker_position_id, close_reason, entry_index, entry_order_type
                ) VALUES (
                    :id, :signal_id, :user_id, 1, 1, 'error',
                    '1845153776', 'broker_filled_position_not_visible', 1, 'market'
                )
                RETURNING id
                """
            ),
            {"id": uuid4(), "signal_id": signal_id, "user_id": owner_id},
        ).scalar_one()
        _make_deal(
            session,
            user_id=owner_id,
            mt5_account_id=mt5_account_id,
            broker_client_id="SS_unrelated_marker",
            broker_position_id="1845153776",
            entry_type="DEAL_ENTRY_IN",
            occurred_at=datetime(2026, 8, 25, 6, 10, 29, tzinfo=UTC),
            volume="0.01",
            price="4650.00",
        )
        session.commit()

    service = BrokerFillSettlementService(session_factory)
    result = service.settle_once()

    assert result.adopted_open == 1
    with session_factory() as session:
        row = session.execute(
            text("SELECT status FROM positions WHERE id=:id"), {"id": position_id}
        ).mappings().one()
    assert row["status"] == "open"
