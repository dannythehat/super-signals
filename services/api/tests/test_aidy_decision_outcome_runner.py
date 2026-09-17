"""Outcome scoring against real tables: the baseline is real, the write is once-only.

These use PostgreSQL directly for the same reason test_aidy_decision_ledger.py does --
the selection join against provider_trade_scores and the append-only trigger are SQL,
not something a mock can exercise honestly.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from app.aidy_decision_outcome_runner import AidyDecisionOutcomeRunner

DATABASE_URL = os.getenv("DATABASE_URL")
API_ROOT = Path(__file__).resolve().parents[1]
OBSERVED_AT = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


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
def session_factory(conn):
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=conn, future=True, expire_on_commit=False)


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
            "title": "Outcome Scoring Test Group",
        },
    )
    return source_id


def add_observation(conn, source_id: UUID, *, side="BUY", index=0) -> UUID:
    message_id, observation_id = uuid4(), uuid4()
    conn.execute(
        text(
            "INSERT INTO messages (id,source_id,telegram_message_id,raw_text,posted_at,"
            "ingestion_status,content_sha256,created_at) "
            "VALUES (:id,:source_id,:tg,'BUY gold',:posted,'received',:sha,now())"
        ),
        {
            "id": message_id,
            "source_id": source_id,
            "tg": 2000 + index,
            "posted": OBSERVED_AT,
            "sha": f"{index:064d}",
        },
    )
    conn.execute(
        text(
            "INSERT INTO provider_trade_observations "
            "(id,message_id,source_id,revision_index,observed_at,decision,action,executable,"
            "outcome_reason,side,symbol,entry_low,entry_high,stop_loss,take_profits,"
            "decision_source,raw_text_sha256) "
            "VALUES (:id,:message,:source,0,:observed,'new_trade','skip',false,'missing_sl',"
            ":side,'XAUUSD',4000,4000,3990,'[\"4010\"]','openai',:sha)"
        ),
        {
            "id": observation_id,
            "message": message_id,
            "source": source_id,
            "observed": OBSERVED_AT,
            "side": side,
            "sha": f"{index:064d}",
        },
    )
    return observation_id


def add_decision(conn, observation_id: UUID, source_id: UUID, *, decision_class: str) -> UUID:
    decision_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO aidy_decisions (id,observation_id,source_id,signal_posted_at,decided_at,"
            "decision_class,reasons,model_version,rule_version,evidence_digest) "
            "VALUES (:id,:obs,:source,:posted,now(),:decision_class,'[]','m','r','d')"
        ),
        {
            "id": decision_id,
            "obs": observation_id,
            "source": source_id,
            "posted": OBSERVED_AT,
            "decision_class": decision_class,
        },
    )
    return decision_id


def add_score(
    conn,
    observation_id: UUID,
    source_id: UUID,
    *,
    outcome: str,
    net_pnl_usd=None,
    realized_r=None,
    unresolvable_reason=None,
):
    conn.execute(
        text(
            "INSERT INTO provider_trade_scores "
            "(id,observation_id,source_id,benchmark_model,entry_convention,outcome,"
            "unresolvable_reason,net_pnl_usd,realized_r) "
            "VALUES (gen_random_uuid(),:obs,:source,'m','zone',:outcome,:reason,:pnl,:r)"
        ),
        {
            "obs": observation_id,
            "source": source_id,
            "outcome": outcome,
            "reason": unresolvable_reason,
            "pnl": net_pnl_usd,
            "r": realized_r,
        },
    )


def test_a_denied_trade_that_really_lost_gets_scored_confirmed_helped(
    conn, source_id, session_factory
) -> None:
    observation_id = add_observation(conn, source_id, index=1)
    decision_id = add_decision(conn, observation_id, source_id, decision_class="deny")
    add_score(conn, observation_id, source_id, outcome="lost", net_pnl_usd=-40, realized_r=-1)

    runner = AidyDecisionOutcomeRunner(session_factory)
    summary = asyncio.run(runner.run())

    assert summary.written == 1
    row = conn.execute(
        text(
            "SELECT decision_delta_usd, resolution FROM aidy_decision_outcomes "
            "WHERE decision_id=:id"
        ),
        {"id": decision_id},
    ).mappings().one()
    assert row["resolution"] == "confirmed_helped"
    assert row["decision_delta_usd"] == 40


def test_an_unresolvable_trade_is_not_selected_yet(conn, source_id, session_factory) -> None:
    observation_id = add_observation(conn, source_id, index=2)
    add_decision(conn, observation_id, source_id, decision_class="approve")
    add_score(
        conn,
        observation_id,
        source_id,
        outcome="unresolvable",
        unresolvable_reason="research_fetch_failed:ValueError",
    )

    runner = AidyDecisionOutcomeRunner(session_factory)
    summary = asyncio.run(runner.run())

    assert summary.selected == 0


def test_an_already_scored_decision_is_never_reselected(
    conn, source_id, session_factory
) -> None:
    observation_id = add_observation(conn, source_id, index=3)
    add_decision(conn, observation_id, source_id, decision_class="approve")
    add_score(conn, observation_id, source_id, outcome="won", net_pnl_usd=20, realized_r=1)

    runner = AidyDecisionOutcomeRunner(session_factory)
    first = asyncio.run(runner.run())
    second = asyncio.run(runner.run())

    assert first.written == 1
    assert second.selected == 0, (
        "an outcome row already exists -- this decision must not be re-scored"
    )


def test_an_outcome_row_cannot_be_rewritten(conn, source_id) -> None:
    observation_id = add_observation(conn, source_id, index=4)
    decision_id = add_decision(conn, observation_id, source_id, decision_class="approve")
    outcome_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO aidy_decision_outcomes "
            "(id,decision_id,baseline_pnl_usd,baseline_realized_r,actual_pnl_usd,"
            "actual_realized_r,decision_delta_usd,resolved_at,resolution) "
            "VALUES (:id,:decision,0,0,0,0,0,now(),'neutral')"
        ),
        {"id": outcome_id, "decision": decision_id},
    )

    with pytest.raises(DBAPIError), conn.begin_nested():
        conn.execute(
            text("UPDATE aidy_decision_outcomes SET resolution='confirmed_helped' WHERE id=:id"),
            {"id": outcome_id},
        )
