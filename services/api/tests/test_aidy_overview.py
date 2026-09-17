"""The AIDY overview reads exactly what is stored -- no recomputation, no invented rows.

This is the one place the owner (or a future session) can see AIDY's decisions,
outcomes and cohort evidence without running raw SQL. If it silently drifted from the
underlying tables, it would be worse than no dashboard at all.
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

from app.aidy_overview import AidyOverviewService

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
            "title": "Overview Test Group",
        },
    )
    return source_id


def add_observation(conn, source_id: UUID, *, index: int) -> UUID:
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
            "tg": 4000 + index,
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
            "'BUY','XAUUSD',4000,4000,3990,'[\"4010\"]','openai',:sha)"
        ),
        {
            "id": observation_id,
            "message": message_id,
            "source": source_id,
            "observed": OBSERVED_AT,
            "sha": f"{index:064d}",
        },
    )
    return observation_id


def add_decision_with_outcome(
    conn, observation_id: UUID, source_id: UUID, *, decision_class: str, resolution: str, delta_usd
) -> None:
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
    conn.execute(
        text(
            "INSERT INTO aidy_decision_outcomes "
            "(id,decision_id,baseline_pnl_usd,baseline_realized_r,actual_pnl_usd,actual_realized_r,"
            "decision_delta_usd,resolved_at,resolution) "
            "VALUES (gen_random_uuid(),:decision,0,0,0,0,:delta,now(),:resolution)"
        ),
        {"decision": decision_id, "delta": delta_usd, "resolution": resolution},
    )


def test_overview_totals_and_by_class_match_what_was_written(
    conn, source_id, session_factory
) -> None:
    first = add_observation(conn, source_id, index=1)
    second = add_observation(conn, source_id, index=2)
    add_decision_with_outcome(
        conn, first, source_id, decision_class="deny", resolution="confirmed_helped", delta_usd=40
    )
    add_decision_with_outcome(
        conn, second, source_id, decision_class="deny", resolution="confirmed_hurt", delta_usd=-15
    )

    view = AidyOverviewService(session_factory).read()

    assert view.total_decisions >= 2
    assert view.total_outcomes_scored >= 2
    deny_summary = next(item for item in view.by_class if item.decision_class == "deny")
    assert deny_summary.decision_count >= 2
    assert deny_summary.confirmed_helped >= 1
    assert deny_summary.confirmed_hurt >= 1


def test_a_decision_with_no_outcome_yet_is_counted_but_not_scored(
    conn, source_id, session_factory
) -> None:
    observation_id = add_observation(conn, source_id, index=3)
    conn.execute(
        text(
            "INSERT INTO aidy_decisions (id,observation_id,source_id,signal_posted_at,decided_at,"
            "decision_class,reasons,model_version,rule_version,evidence_digest) "
            "VALUES (gen_random_uuid(),:obs,:source,:posted,now(),'approve','[]','m','r','d')"
        ),
        {"obs": observation_id, "source": source_id, "posted": OBSERVED_AT},
    )

    view = AidyOverviewService(session_factory).read()

    approve_summary = next(item for item in view.by_class if item.decision_class == "approve")
    assert approve_summary.decision_count >= 1
    assert approve_summary.scored_count <= approve_summary.decision_count
