"""Fingerprint computation and append-only persistence against real tables.

Same reason the other Decision Ledger integration suites use PostgreSQL directly: the
geometry aggregation, the cohort HAVING-clause sample floor, and the append-only trigger
are SQL, not something a mock can exercise honestly.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from app.provider_fingerprint_engine import ProviderFingerprintEngine
from app.provider_fingerprint_runner import ProviderFingerprintRunner

DATABASE_URL = os.getenv("DATABASE_URL")
API_ROOT = Path(__file__).resolve().parents[1]
BASE_TIME = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


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
            "title": "Fingerprint Test Group",
        },
    )
    conn.execute(
        text(
            "INSERT INTO provider_research_profiles "
            "(source_id,research_state,style,observed_messages,signal_like_messages,"
            "structured_signal_messages,management_messages,edited_messages,signal_likelihood,"
            "interpretation_readiness,last_scan_at,created_at,updated_at) "
            "VALUES (:source,'shadow','scalper',10,10,10,0,0,1.0,1.0,now(),now(),now())"
        ),
        {"source": source_id},
    )
    return source_id


def add_resolved_trade(
    conn,
    source_id: UUID,
    *,
    index: int,
    side: str,
    outcome: str,
    entry: float,
    stop: float,
    tp: float,
    pnl: float,
) -> None:
    message_id, observation_id = uuid4(), uuid4()
    posted_at = BASE_TIME + timedelta(hours=index)
    conn.execute(
        text(
            "INSERT INTO messages (id,source_id,telegram_message_id,raw_text,posted_at,"
            "ingestion_status,content_sha256,created_at) "
            "VALUES (:id,:source_id,:tg,'signal',:posted,'received',:sha,now())"
        ),
        {
            "id": message_id,
            "source_id": source_id,
            "tg": 9000 + index,
            "posted": posted_at,
            "sha": f"{index:064d}",
        },
    )
    conn.execute(
        text(
            "INSERT INTO provider_trade_observations "
            "(id,message_id,source_id,revision_index,observed_at,decision,action,executable,"
            "outcome_reason,side,symbol,entry_low,entry_high,stop_loss,take_profits,"
            "decision_source,raw_text_sha256) "
            "VALUES (:id,:message,:source,0,:observed,'new_trade','execute',true,'complete',"
            ":side,'XAUUSD',:entry,:entry,:stop,:tp,'openai',:sha)"
        ),
        {
            "id": observation_id,
            "message": message_id,
            "source": source_id,
            "observed": posted_at,
            "side": side,
            "entry": entry,
            "stop": stop,
            "tp": f'["{tp}"]',
            "sha": f"{index:064d}",
        },
    )
    conn.execute(
        text(
            "INSERT INTO provider_trade_scores "
            "(id,observation_id,source_id,benchmark_model,entry_convention,outcome,"
            "net_pnl_usd,realized_r) "
            "VALUES (gen_random_uuid(),:obs,:source,'m','zone',:outcome,:pnl,:r)"
        ),
        {
            "obs": observation_id,
            "source": source_id,
            "outcome": outcome,
            "pnl": pnl,
            "r": pnl / 10,
        },
    )


def test_below_geometry_floor_never_claims_a_stop_comparison(
    conn, source_id, session_factory
) -> None:
    for i in range(3):
        add_resolved_trade(
            conn,
            source_id,
            index=i,
            side="BUY",
            outcome="won",
            entry=2400,
            stop=2390,
            tp=2420,
            pnl=50,
        )
    for i in range(3, 5):
        add_resolved_trade(
            conn,
            source_id,
            index=i,
            side="BUY",
            outcome="lost",
            entry=2400,
            stop=2390,
            tp=2420,
            pnl=-40,
        )

    engine = ProviderFingerprintEngine(session_factory)
    fingerprints = engine.compute_all(minimum_total=1)
    ours = next(fp for fp in fingerprints if fp.source_id == source_id)

    assert ours.geometry_sample_met is False, "3 wins and 2 losses is below the 8-per-outcome floor"
    assert ours.avg_stop_distance_won is None
    assert "Not enough resolved wins and losses" in ours.summary


def test_above_geometry_floor_reports_a_real_won_vs_lost_comparison(
    conn, source_id, session_factory
) -> None:
    for i in range(10):
        add_resolved_trade(
            conn,
            source_id,
            index=i,
            side="BUY",
            outcome="won",
            entry=2400,
            stop=2380,
            tp=2440,
            pnl=80,
        )
    for i in range(10, 20):
        add_resolved_trade(
            conn,
            source_id,
            index=i,
            side="BUY",
            outcome="lost",
            entry=2400,
            stop=2395,
            tp=2440,
            pnl=-20,
        )

    engine = ProviderFingerprintEngine(session_factory)
    fingerprints = engine.compute_all(minimum_total=1)
    ours = next(fp for fp in fingerprints if fp.source_id == source_id)

    assert ours.geometry_sample_met is True
    assert ours.wins == 10
    assert ours.losses == 10
    assert ours.avg_stop_distance_won == 20
    assert ours.avg_stop_distance_lost == 5
    assert "wider stop" in ours.summary
    assert ours.trading_style == "scalper"


def test_runner_persists_one_row_per_provider_and_it_is_append_only(
    conn, source_id, session_factory
) -> None:
    for i in range(10):
        add_resolved_trade(
            conn,
            source_id,
            index=i,
            side="BUY",
            outcome="won",
            entry=2400,
            stop=2380,
            tp=2440,
            pnl=80,
        )

    runner = ProviderFingerprintRunner(session_factory)
    summary = asyncio.run(runner.run(minimum_total=1))

    assert summary.computed >= 1
    row = (
        conn.execute(
            text(
                "SELECT id, research_only, live_money_execution_allowed, descriptive_only "
                "FROM provider_trade_fingerprints WHERE source_id = :source "
                "ORDER BY computed_at DESC LIMIT 1"
            ),
            {"source": source_id},
        )
        .mappings()
        .one()
    )
    assert row["research_only"] is True
    assert row["live_money_execution_allowed"] is False
    assert row["descriptive_only"] is True

    with pytest.raises(DBAPIError), conn.begin_nested():
        conn.execute(
            text("UPDATE provider_trade_fingerprints SET summary='tampered' WHERE id=:id"),
            {"id": row["id"]},
        )
