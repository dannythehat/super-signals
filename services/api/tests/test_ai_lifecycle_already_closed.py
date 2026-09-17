"""``AiLifecycleBridge._resolve_signal`` correctly tells "already closed" from "unresolved".

Real production data (queried 2026-09-17) showed the overwhelming majority of
`lifecycle_event_not_resolved` management-dispatch failures were not a linking bug at
all: the provider's close/breakeven/etc instruction simply arrived after its target
position had already closed by some other path (a stop hit, an earlier close, a
duplicate provider message). Treating that as an unresolved failure meant it was
logged as broken and never actioned, forever, even though there was genuinely nothing
left to do. This links the instruction to that already-closed signal instead, so the
dispatcher's own existing "no exposure anywhere" success path takes over.

Runs against real PostgreSQL because the whole point is the SQL join across
signals/positions -- a mock cannot tell us whether it actually matches real rows.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.ai_lifecycle_bridge import AiLifecycleBridge

DATABASE_URL = os.getenv("DATABASE_URL")
API_ROOT = Path(__file__).resolve().parents[1]
OWNER = uuid4()


@pytest.fixture(scope="module")
def engine():
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")
    engine = create_engine(DATABASE_URL, future=True)
    config = Config(str(API_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    command.upgrade(config, "head")
    yield engine
    engine.dispose()


@pytest.fixture
def conn(engine):
    connection = engine.connect()
    transaction = connection.begin()
    try:
        yield connection
    finally:
        transaction.rollback()
        connection.close()


@pytest.fixture
def source_id(conn) -> UUID:
    user_id, account_id, source_id = uuid4(), uuid4(), uuid4()
    conn.execute(
        text("INSERT INTO users (id,email,status) VALUES (:id,:email,'active')"),
        {"id": user_id, "email": f"{user_id}@example.test"},
    )
    conn.execute(
        text(
            "INSERT INTO telegram_accounts "
            "(id,owner_user_id,label,phone_number_e164,session_ciphertext,session_fingerprint) "
            "VALUES (:id,:owner,'test',:phone,:cipher,:fp)"
        ),
        {
            "id": account_id,
            "owner": user_id,
            "phone": f"+{abs(hash(str(account_id))) % 10**11:011d}",
            "cipher": b"x",
            "fp": str(account_id).replace("-", "")[:16],
        },
    )
    conn.execute(
        text(
            "INSERT INTO sources "
            "(id,telegram_account_id,chat_id,chat_title,source_alias,status,created_at,updated_at) "
            "VALUES (:id,:account,:chat_id,:title,:title,'testing',now(),now())"
        ),
        {
            "id": source_id,
            "account": account_id,
            "chat_id": -abs(hash(str(source_id))) % 10**12,
            "title": "Already Closed Test Group",
        },
    )
    return source_id


@pytest.fixture
def session(conn):
    return sessionmaker(bind=conn, future=True, expire_on_commit=False)()


def _insert_message(conn, *, source_id: UUID, telegram_message_id: int, raw_text: str) -> UUID:
    message_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO messages (id,source_id,telegram_message_id,raw_text,posted_at,"
            "ingestion_status,content_sha256,created_at) "
            "VALUES (:id,:source_id,:tg,:text,now(),'received',:sha,now())"
        ),
        {
            "id": message_id,
            "source_id": source_id,
            "tg": telegram_message_id,
            "text": raw_text,
            "sha": f"{telegram_message_id:064d}",
        },
    )
    return message_id


def _insert_signal(
    conn, *, source_id: UUID, symbol: str, provider_message_id: int, posted_at: datetime
) -> UUID:
    signal_id = uuid4()
    original_message_id = _insert_message(
        conn, source_id=source_id, telegram_message_id=provider_message_id, raw_text="BUY gold"
    )
    conn.execute(
        text(
            "INSERT INTO signals (id,source_message_id,symbol,side,original_text,source_id,"
            "provider_chat_id,provider_message_id,source_posted_at,signal_fingerprint) "
            "VALUES (:id,:message,:symbol,'BUY','BUY gold',:source,9999,:pmid,:posted,:fp)"
        ),
        {
            "id": signal_id,
            "message": original_message_id,
            "symbol": symbol,
            "source": source_id,
            "pmid": provider_message_id,
            "posted": posted_at,
            "fp": str(signal_id),
        },
    )
    return signal_id


def _insert_position(conn, *, signal_id: UUID, status: str, broker_position_id: str | None) -> None:
    conn.execute(
        text(
            "INSERT INTO positions (id,signal_id,user_id,tp_index,status,planned_risk_percent,"
            "broker_position_id) "
            "VALUES (:id,:signal,:user,1,:status,1.0,:broker_position_id)"
        ),
        {
            "id": uuid4(),
            "signal": signal_id,
            "user": OWNER,
            "status": status,
            "broker_position_id": broker_position_id,
        },
    )


def _row(*, source_id: UUID, raw_text: str, occurred_at: datetime) -> dict:
    return {
        "message_id": uuid4(),
        "source_id": source_id,
        "telegram_message_id": 1,
        "raw_text": raw_text,
        "raw_payload": {},
        "occurred_at": occurred_at,
    }


def test_close_instruction_links_to_its_already_closed_position(conn, session, source_id) -> None:
    posted_at = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    occurred_at = datetime(2026, 9, 1, 10, 5, tzinfo=UTC)
    signal_id = _insert_signal(
        conn, source_id=source_id, symbol="XAUUSD", provider_message_id=501, posted_at=posted_at
    )
    _insert_position(conn, signal_id=signal_id, status="closed", broker_position_id="broker-1")

    linked, reason = AiLifecycleBridge._resolve_signal(
        session,
        _row(source_id=source_id, raw_text="Close our trade now", occurred_at=occurred_at),
        revision_index=0,
    )

    assert linked is not None
    assert linked["id"] == signal_id
    assert reason == "management_target_already_closed"


def test_still_open_position_is_found_by_the_existing_active_path_not_this_one(conn, session, source_id) -> None:
    posted_at = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    occurred_at = datetime(2026, 9, 1, 10, 5, tzinfo=UTC)
    signal_id = _insert_signal(
        conn, source_id=source_id, symbol="XAUUSD", provider_message_id=502, posted_at=posted_at
    )
    _insert_position(conn, signal_id=signal_id, status="open", broker_position_id="broker-2")

    linked, reason = AiLifecycleBridge._resolve_signal(
        session,
        _row(source_id=source_id, raw_text="Close our trade now", occurred_at=occurred_at),
        revision_index=0,
    )

    assert linked is not None
    assert linked["id"] == signal_id
    assert reason == "active_broker_unique"


def test_a_never_executed_signal_still_links_via_the_existing_legacy_path_when_unique(
    conn, session, source_id
) -> None:
    """No position row at all (e.g. blocked by provider risk policy) is not 'already
    closed' -- there is nothing to have closed. It correctly falls through to the
    pre-existing legacy resolver, which still safely links it when unique (and that,
    too, only ever reaches the dispatcher's no-op 'no exposure' success path)."""
    posted_at = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    occurred_at = datetime(2026, 9, 1, 10, 5, tzinfo=UTC)
    signal_id = _insert_signal(
        conn, source_id=source_id, symbol="XAUUSD", provider_message_id=503, posted_at=posted_at
    )

    linked, reason = AiLifecycleBridge._resolve_signal(
        session,
        _row(source_id=source_id, raw_text="Close our trade now", occurred_at=occurred_at),
        revision_index=0,
    )

    assert linked is not None
    assert linked["id"] == signal_id
    assert reason == "standalone_legacy_unique"


def test_a_source_with_no_signal_history_at_all_stays_genuinely_unresolved(
    conn, session, source_id
) -> None:
    """Nothing exists to link to -- neither an already-closed position nor any
    never-executed signal -- so this must still fail closed rather than invent one."""
    occurred_at = datetime(2026, 9, 1, 10, 5, tzinfo=UTC)

    linked, reason = AiLifecycleBridge._resolve_signal(
        session,
        _row(source_id=source_id, raw_text="Close our trade now", occurred_at=occurred_at),
        revision_index=0,
    )

    assert linked is None
    assert reason == "signal_link_unresolved"


def test_already_closed_match_still_respects_the_symbol_hint(conn, session, source_id) -> None:
    posted_at = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    occurred_at = datetime(2026, 9, 1, 10, 5, tzinfo=UTC)
    gold_signal = _insert_signal(
        conn, source_id=source_id, symbol="XAUUSD", provider_message_id=504, posted_at=posted_at
    )
    _insert_position(conn, signal_id=gold_signal, status="closed", broker_position_id="broker-4a")
    silver_signal = _insert_signal(
        conn, source_id=source_id, symbol="XAGUSD", provider_message_id=505, posted_at=posted_at
    )
    _insert_position(conn, signal_id=silver_signal, status="closed", broker_position_id="broker-4b")

    linked, reason = AiLifecycleBridge._resolve_signal(
        session,
        _row(source_id=source_id, raw_text="XAGUSD close our trade now", occurred_at=occurred_at),
        revision_index=0,
    )

    assert linked is not None
    assert linked["id"] == silver_signal
    assert reason == "management_target_already_closed"


def test_a_position_still_open_for_another_user_on_the_same_signal_is_never_masked(
    conn, session, source_id
) -> None:
    """One member's residual open position on a signal must never be treated as
    'already closed' just because the owner's own leg happens to be closed. This is
    actually caught even earlier than the new query added here: the primary active-
    position lookup above is not scoped to one user, so any open position on the
    signal -- owner's or a member's -- already routes it to the existing, correct
    'active_broker_unique' path before the already-closed check is ever reached."""
    posted_at = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    occurred_at = datetime(2026, 9, 1, 10, 5, tzinfo=UTC)
    signal_id = _insert_signal(
        conn, source_id=source_id, symbol="XAUUSD", provider_message_id=506, posted_at=posted_at
    )
    _insert_position(conn, signal_id=signal_id, status="closed", broker_position_id="broker-6a")
    conn.execute(
        text(
            "INSERT INTO positions (id,signal_id,user_id,tp_index,status,planned_risk_percent,"
            "broker_position_id) VALUES (:id,:signal,:user,1,'open',1.0,'broker-6b')"
        ),
        {"id": uuid4(), "signal": signal_id, "user": uuid4()},
    )

    linked, reason = AiLifecycleBridge._resolve_signal(
        session,
        _row(source_id=source_id, raw_text="Close our trade now", occurred_at=occurred_at),
        revision_index=0,
    )

    assert linked is not None
    assert linked["id"] == signal_id
    assert reason == "active_broker_unique"
