"""Research capture must not be gated by whether a trade was executable.

A Signal is only created when a trade can also be mirrored to a broker. Before this
contract existed, a trade the interpreter understood but could not execute left no
research trace, so whole providers were invisible to shadow research. Production on
2026-09-14 held 2,206 understood new trades and 6,637 understood updates that produced
nothing; The Gold Club alone posted 508 understood trades and zero signals because it
does not publish a stop loss.

These tests pin the opposite: every understood trade is recorded, the record states
truthfully whether it was executable, and the table cannot be turned into an execution
input.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from app.ai_message_pipeline import AiMessagePipeline
from app.ai_message_supervisor import AiMessageDecision

DATABASE_URL = os.getenv("DATABASE_URL")
API_ROOT = Path(__file__).resolve().parents[1]

# The exact shape production skipped: a complete, correctly understood SELL that the
# provider published without a stop loss.
NO_STOP_LOSS_TRADE = {
    "side": "SELL",
    "symbol": "XAUUSD",
    "entry_low": "4292",
    "entry_high": "4296",
    "stop_loss": None,
    "take_profits": [],
    "order_type": "market",
    "tp_open": False,
}


def _decision(**overrides) -> AiMessageDecision:
    base = {
        "decision": "new_trade",
        "action": "skip",
        "confidence": 0.72,
        "reason": "missing_sl",
        "extracted": dict(NO_STOP_LOSS_TRADE),
        "model": "gpt-5-mini-2025-08-07",
        "response_id": None,
        "latency_ms": 10,
        "source": "openai",
        "raw_text_sha256": "a" * 64,
    }
    base.update(overrides)
    return AiMessageDecision(**base)


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
    """One rolled-back transaction per test.

    Observations are append-only by design, so a test that committed rows could not
    clean up after itself and would leave data behind for every later test in the
    database. Everything here happens inside a transaction that is always rolled back.
    """
    connection = engine.connect()
    transaction = connection.begin()
    try:
        yield connection
    finally:
        transaction.rollback()
        connection.close()


@pytest.fixture
def seeded_message(conn):
    """A source and message to attach observations to."""
    user_id, account_id = uuid4(), uuid4()
    source_id, message_id = uuid4(), uuid4()
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
            "VALUES (:id,:account,:chat_id,:title,:title,'shadow',now(),now())"
        ),
        {
            "id": source_id,
            "account": account_id,
            "chat_id": -abs(hash(str(source_id))) % 10**12,
            "title": "Test Group",
        },
    )
    conn.execute(
        text(
            "INSERT INTO messages (id,source_id,telegram_message_id,raw_text,posted_at,"
            "ingestion_status,content_sha256,created_at) "
            "VALUES (:id,:source_id,1,:raw,now(),'received',:sha,now())"
        ),
        {"id": message_id, "source_id": source_id, "raw": "SELL gold 4292-4296", "sha": "b" * 64},
    )
    return source_id, message_id


def _observations(conn, message_id: UUID) -> list[dict]:
    rows = conn.execute(
        text("SELECT * FROM provider_trade_observations WHERE message_id=:m"),
        {"m": message_id},
    ).mappings().all()
    return [dict(row) for row in rows]


def test_understood_but_unexecutable_trade_is_still_recorded(conn, seeded_message) -> None:
    """The regression that made whole providers invisible for five weeks."""
    source_id, message_id = seeded_message
    AiMessagePipeline._store_observation(conn, message_id, 0, _decision())

    observed = _observations(conn, message_id)
    assert len(observed) == 1, "a skipped trade must still leave a research trace"
    row = observed[0]
    assert row["executable"] is False
    assert row["outcome_reason"] == "missing_sl"
    assert row["decision"] == "new_trade"
    assert row["source_id"] == source_id
    # The trade itself is preserved, not just the fact that one existed.
    assert row["side"] == "SELL"
    assert row["symbol"] == "XAUUSD"
    assert row["entry_low"] == 4292
    assert row["entry_high"] == 4296
    assert row["stop_loss"] is None


def test_executable_trade_is_recorded_as_executable(conn, seeded_message) -> None:
    _, message_id = seeded_message
    executable = dict(NO_STOP_LOSS_TRADE, stop_loss="4300", take_profits=["4280", "4270"])
    AiMessagePipeline._store_observation(
            conn, message_id, 0, _decision(action="execute", reason="ok", extracted=executable)
        )

    row = _observations(conn, message_id)[0]
    assert row["executable"] is True
    assert row["stop_loss"] == 4300
    assert row["take_profits"] == ["4280", "4270"]


def test_ignored_management_message_is_recorded(conn, seeded_message) -> None:
    """6,637 updates were understood and ignored; they are how a group manages trades."""
    _, message_id = seeded_message
    update = {"update_type": "move_sl", "update_target": "breakeven", "update_value": "4300"}
    AiMessagePipeline._store_observation(
            conn,
            message_id,
            0,
            _decision(
                decision="trade_update",
                action="ignore",
                reason="no_open_trade",
                extracted=update,
            ),
        )

    row = _observations(conn, message_id)[0]
    assert row["decision"] == "trade_update"
    assert row["executable"] is False
    assert row["update_type"] == "move_sl"
    assert row["update_target"] == "breakeven"


def test_chatter_is_not_recorded_as_a_trade_observation(conn, seeded_message) -> None:
    _, message_id = seeded_message
    AiMessagePipeline._store_observation(
            conn, message_id, 0, _decision(decision="chatter", action="ignore", extracted={})
        )

    assert _observations(conn, message_id) == []


def test_recording_is_idempotent_per_revision(conn, seeded_message) -> None:
    _, message_id = seeded_message
    AiMessagePipeline._store_observation(conn, message_id, 0, _decision())
    AiMessagePipeline._store_observation(conn, message_id, 0, _decision())

    assert len(_observations(conn, message_id)) == 1


@pytest.mark.parametrize(
    ("column", "value"),
    [("research_only", "false"), ("live_execution_affected", "true")],
)
def test_observations_cannot_become_an_execution_input(conn, seeded_message, column, value) -> None:
    """The research-only boundary is structural, not documentary."""
    _, message_id = seeded_message
    AiMessagePipeline._store_observation(conn, message_id, 0, _decision())

    # A rejected statement aborts its transaction, so each attempt gets a savepoint.
    with pytest.raises(DBAPIError), conn.begin_nested():
        conn.execute(
            text(
                "INSERT INTO provider_trade_observations "
                "(id,message_id,source_id,revision_index,observed_at,decision,action,"
                f"executable,outcome_reason,decision_source,raw_text_sha256,{column}) "
                "SELECT gen_random_uuid(),m.id,m.source_id,9,now(),'new_trade','execute',"
                f"true,'x','openai',repeat('c',64),{value} FROM messages m WHERE m.id=:m"
            ),
            {"m": message_id},
        )


def test_executable_row_cannot_claim_an_action_that_creates_no_signal(conn, seeded_message) -> None:
    _, message_id = seeded_message

    with pytest.raises(DBAPIError), conn.begin_nested():
        conn.execute(
            text(
                "INSERT INTO provider_trade_observations "
                "(id,message_id,source_id,revision_index,observed_at,decision,action,"
                "executable,outcome_reason,decision_source,raw_text_sha256) "
                "SELECT gen_random_uuid(),m.id,m.source_id,8,now(),'new_trade','skip',"
                "true,'x','openai',repeat('d',64) FROM messages m WHERE m.id=:m"
            ),
            {"m": message_id},
        )


def test_observed_history_cannot_be_rewritten(conn, seeded_message) -> None:
    _, message_id = seeded_message
    AiMessagePipeline._store_observation(conn, message_id, 0, _decision())

    with pytest.raises(DBAPIError), conn.begin_nested():
        conn.execute(
            text(
                "UPDATE provider_trade_observations SET outcome_reason='rewritten' "
                "WHERE message_id=:m"
            ),
            {"m": message_id},
        )
