"""The cohort scoreboard must split what the blended one hides, and stay honest doing it.

Two trades from the same provider, same side, but one in the London hours and one late,
must land in two different rows -- otherwise the view is not adding anything the blended
scoreboard didn't already have. And a cell most of whose trades are still open must still
report how thin it is (scored_coverage_pct), not just a clean-looking win rate.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

DATABASE_URL = os.getenv("DATABASE_URL")
API_ROOT = Path(__file__).resolve().parents[1]

# A Tuesday, comfortably inside the London session window (10:00 UTC).
LONDON_TUESDAY = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)
# The same Tuesday, comfortably inside the 'late' bucket (23:00 UTC).
LATE_TUESDAY = datetime(2026, 9, 8, 23, 0, tzinfo=UTC)


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
            "title": "Cohort Test Group",
        },
    )
    return source_id


def add_scored_observation(
    conn,
    source_id: UUID,
    *,
    observed_at: datetime,
    side: str,
    outcome: str,
    net_pnl_usd,
    index: int,
) -> UUID:
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
            "tg": 3000 + index,
            "posted": observed_at,
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
            "observed": observed_at,
            "side": side,
            "sha": f"{index:064d}",
        },
    )
    conn.execute(
        text(
            "INSERT INTO provider_trade_scores "
            "(id,observation_id,source_id,benchmark_model,entry_convention,outcome,"
            "net_pnl_usd,realized_r) "
            "VALUES (gen_random_uuid(),:obs,:source,'m','zone',:outcome,:pnl,0)"
        ),
        {"obs": observation_id, "source": source_id, "outcome": outcome, "pnl": net_pnl_usd},
    )
    return observation_id


def test_the_same_side_in_different_sessions_lands_in_different_rows(conn, source_id) -> None:
    add_scored_observation(
        conn,
        source_id,
        observed_at=LONDON_TUESDAY,
        side="BUY",
        outcome="won",
        net_pnl_usd=50,
        index=1,
    )
    add_scored_observation(
        conn,
        source_id,
        observed_at=LATE_TUESDAY,
        side="BUY",
        outcome="lost",
        net_pnl_usd=-20,
        index=2,
    )

    rows = conn.execute(
        text(
            "SELECT session, weekday, wins, losses, net_pnl_usd "
            "FROM provider_trade_scoreboard_by_cohort WHERE source_id=:id ORDER BY session"
        ),
        {"id": source_id},
    ).mappings().all()

    by_session = {row["session"]: row for row in rows}
    assert by_session["london"]["wins"] == 1
    assert by_session["london"]["net_pnl_usd"] == 50
    assert by_session["late"]["losses"] == 1
    assert by_session["late"]["net_pnl_usd"] == -20
    assert by_session["london"]["weekday"] == "Tuesday"


def test_a_thin_cohort_still_reports_how_thin_it_is(conn, source_id) -> None:
    add_scored_observation(
        conn,
        source_id,
        observed_at=LONDON_TUESDAY,
        side="SELL",
        outcome="won",
        net_pnl_usd=10,
        index=3,
    )
    add_scored_observation(
        conn,
        source_id,
        observed_at=LONDON_TUESDAY + timedelta(minutes=5),
        side="SELL",
        outcome="open_at_window_end",
        net_pnl_usd=None,
        index=4,
    )

    row = conn.execute(
        text(
            "SELECT trades_scorable, trades_resolved, scored_coverage_pct "
            "FROM provider_trade_scoreboard_by_cohort "
            "WHERE source_id=:id AND side='SELL' AND session='london'"
        ),
        {"id": source_id},
    ).mappings().one()

    assert row["trades_scorable"] == 2
    assert row["trades_resolved"] == 1, "the still-open trade must not count as resolved"
    assert row["scored_coverage_pct"] == 50.0
