"""Regression coverage for edited Telegram provider signals in retrospective scoring.

A provider may progressively edit one Telegram message from a bare direction into a
complete trade. Those revisions are evidence about one logical setup, not independent
trades, and no revision may receive market history from before that revision existed.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app.provider_fairness import BENCHMARK_MODEL
from app.provider_trade_scoring_runner import (
    RETROSPECTIVE_BENCHMARK_MODEL,
    _SELECTABLE,
)

DATABASE_URL = os.getenv("DATABASE_URL")
API_ROOT = Path(__file__).resolve().parents[1]
POSTED_AT = datetime(2026, 9, 16, 10, 0, 15, tzinfo=UTC)


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
            "VALUES (:id,:owner,'canonical-edit-test',:phone,:cipher,:fp)"
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
            "VALUES (:id,:account,:chat_id,'Canonical Edit Test','Canonical Edit Test',"
            "'shadow',now(),now())"
        ),
        {
            "id": source_id,
            "account": account_id,
            "chat_id": -abs(hash(str(source_id))) % 10**12,
        },
    )
    return source_id


def add_message(conn, source_id: UUID, *, index: int, posted_at: datetime = POSTED_AT) -> UUID:
    message_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO messages (id,source_id,telegram_message_id,raw_text,posted_at,"
            "ingestion_status,content_sha256,created_at) "
            "VALUES (:id,:source,:tg,'BUY gold',:posted,'received',:sha,now())"
        ),
        {
            "id": message_id,
            "source": source_id,
            "tg": 900000 + index,
            "posted": posted_at,
            "sha": f"{index:064d}",
        },
    )
    return message_id


def add_revision(conn, message_id: UUID, revision_index: int, edited_at: datetime) -> None:
    conn.execute(
        text(
            "INSERT INTO message_revisions "
            "(id,message_id,revision_index,raw_text,raw_payload,edited_at,content_sha256,created_at) "
            "VALUES (gen_random_uuid(),:message,:revision,'edited trade','{}'::jsonb,:edited,:sha,:created)"
        ),
        {
            "message": message_id,
            "revision": revision_index,
            "edited": edited_at,
            "created": edited_at + timedelta(seconds=2),
            "sha": f"{revision_index:064d}",
        },
    )


def add_observation(
    conn,
    source_id: UUID,
    message_id: UUID,
    *,
    revision_index: int,
    entry_low: str | None = "4000",
    entry_high: str | None = "4000",
    stop_loss: str | None = "3990",
    take_profits: str = '["4010"]',
    created_at: datetime | None = None,
) -> UUID:
    observation_id = uuid4()
    created_at = created_at or POSTED_AT + timedelta(seconds=revision_index)
    conn.execute(
        text(
            "INSERT INTO provider_trade_observations "
            "(id,message_id,source_id,revision_index,observed_at,decision,action,executable,"
            "outcome_reason,side,entry_low,entry_high,stop_loss,take_profits,"
            "decision_source,raw_text_sha256,created_at) "
            "VALUES (:id,:message,:source,:revision,:observed,'new_trade','skip',false,'test',"
            "'BUY',:low,:high,:stop,CAST(:tps AS jsonb),'openai',:sha,:created)"
        ),
        {
            "id": observation_id,
            "message": message_id,
            "source": source_id,
            "revision": revision_index,
            # This deliberately remains the original post time, matching production.
            "observed": POSTED_AT,
            "low": entry_low,
            "high": entry_high,
            "stop": stop_loss,
            "tps": take_profits,
            "sha": str(observation_id).replace("-", "").ljust(64, "0")[:64],
            "created": created_at,
        },
    )
    return observation_id


def add_score(
    conn,
    observation_id: UUID,
    source_id: UUID,
    *,
    model: str,
    outcome: str,
    pnl: Decimal | None,
    realized_r: Decimal | None = None,
    legs_total: int = 1,
) -> None:
    conn.execute(
        text(
            "INSERT INTO provider_trade_scores "
            "(id,observation_id,source_id,scored_at,benchmark_model,entry_convention,outcome,"
            "entry_price,net_pnl_usd,realized_r,legs_resolved,legs_total,bars_replayed,"
            "missing_minutes) "
            "VALUES (gen_random_uuid(),:observation,:source,now(),:model,'zone',:outcome,4000,"
            ":pnl,:r,:resolved,:legs,5,0)"
        ),
        {
            "observation": observation_id,
            "source": source_id,
            "model": model,
            "outcome": outcome,
            "pnl": pnl,
            "r": realized_r,
            "resolved": legs_total if outcome in {"won", "lost", "breakeven"} else 0,
            "legs": legs_total,
        },
    )


def test_progressive_edits_become_one_trade_at_first_actionable_edit(conn, source_id) -> None:
    message_id = add_message(conn, source_id, index=1)
    # Original post and revision 1 are incomplete.
    add_observation(
        conn,
        source_id,
        message_id,
        revision_index=0,
        stop_loss=None,
        take_profits="[]",
    )
    edit_1 = POSTED_AT + timedelta(seconds=35)
    add_revision(conn, message_id, 1, edit_1)
    add_observation(
        conn,
        source_id,
        message_id,
        revision_index=1,
        take_profits="[]",
        created_at=edit_1 + timedelta(seconds=2),
    )

    # TP1 makes revision 2 first-actionable. TP2 arrives later in the same Telegram post.
    first_actionable_at = POSTED_AT + timedelta(seconds=75)
    add_revision(conn, message_id, 2, first_actionable_at)
    first_actionable = add_observation(
        conn,
        source_id,
        message_id,
        revision_index=2,
        take_profits='["4010"]',
        created_at=first_actionable_at + timedelta(seconds=2),
    )
    later_edit_at = POSTED_AT + timedelta(seconds=105)
    add_revision(conn, message_id, 3, later_edit_at)
    later_revision = add_observation(
        conn,
        source_id,
        message_id,
        revision_index=3,
        take_profits='["4010","4020"]',
        created_at=later_edit_at + timedelta(seconds=2),
    )

    rows = conn.execute(
        text(
            "SELECT id,revision_index,observed_at FROM provider_trade_canonical_observations "
            "WHERE message_id=:message"
        ),
        {"message": message_id},
    ).mappings().all()
    assert len(rows) == 1
    assert UUID(str(rows[0]["id"])) == first_actionable
    assert UUID(str(rows[0]["id"])) != later_revision
    assert rows[0]["revision_index"] == 2
    assert rows[0]["observed_at"] == first_actionable_at

    selectable = conn.execute(text(_SELECTABLE)).mappings().all()
    matching = [row for row in selectable if row["source_id"] == source_id]
    assert len(matching) == 1
    assert UUID(str(matching[0]["id"])) == first_actionable
    assert matching[0]["observed_at"] == first_actionable_at


def test_complete_original_message_uses_original_post_time(conn, source_id) -> None:
    message_id = add_message(conn, source_id, index=2)
    observation_id = add_observation(conn, source_id, message_id, revision_index=0)

    row = conn.execute(
        text(
            "SELECT id,observed_at FROM provider_trade_canonical_observations "
            "WHERE message_id=:message"
        ),
        {"message": message_id},
    ).mappings().one()
    assert UUID(str(row["id"])) == observation_id
    assert row["observed_at"] == POSTED_AT


def test_old_score_generation_is_forced_through_repaired_model(conn, source_id) -> None:
    message_id = add_message(conn, source_id, index=3)
    observation_id = add_observation(conn, source_id, message_id, revision_index=0)
    add_score(
        conn,
        observation_id,
        source_id,
        model=BENCHMARK_MODEL,
        outcome="won",
        pnl=Decimal("10.00"),
        realized_r=Decimal("1"),
    )

    selected = conn.execute(text(_SELECTABLE)).mappings().all()
    assert observation_id in {UUID(str(row["id"])) for row in selected}


def test_settled_repaired_score_is_not_reselected(conn, source_id) -> None:
    message_id = add_message(conn, source_id, index=4)
    observation_id = add_observation(conn, source_id, message_id, revision_index=0)
    add_score(
        conn,
        observation_id,
        source_id,
        model=RETROSPECTIVE_BENCHMARK_MODEL,
        outcome="won",
        pnl=Decimal("10.00"),
        realized_r=Decimal("1"),
    )

    selected = conn.execute(text(_SELECTABLE)).mappings().all()
    assert observation_id not in {UUID(str(row["id"])) for row in selected}


def test_scoreboard_counts_logical_trades_and_separates_open_partial_pnl(conn, source_id) -> None:
    settled_message = add_message(conn, source_id, index=5)
    settled = add_observation(conn, source_id, settled_message, revision_index=0)
    # A later revision of the same Telegram message must not become another catalogue trade.
    later_at = POSTED_AT + timedelta(minutes=1)
    add_revision(conn, settled_message, 1, later_at)
    add_observation(
        conn,
        source_id,
        settled_message,
        revision_index=1,
        take_profits='["4010","4020"]',
        created_at=later_at + timedelta(seconds=1),
    )
    add_score(
        conn,
        settled,
        source_id,
        model=RETROSPECTIVE_BENCHMARK_MODEL,
        outcome="won",
        pnl=Decimal("20.00"),
        realized_r=Decimal("2"),
        legs_total=2,
    )

    open_message = add_message(conn, source_id, index=6, posted_at=POSTED_AT + timedelta(hours=1))
    open_observation = add_observation(
        conn,
        source_id,
        open_message,
        revision_index=0,
        created_at=POSTED_AT + timedelta(hours=1),
    )
    add_score(
        conn,
        open_observation,
        source_id,
        model=RETROSPECTIVE_BENCHMARK_MODEL,
        outcome="open_at_window_end",
        pnl=Decimal("35.00"),
        realized_r=Decimal("3.5"),
    )

    row = conn.execute(
        text("SELECT * FROM provider_trade_scoreboard WHERE source_id=:source"),
        {"source": source_id},
    ).mappings().one()
    assert row["trades_recorded"] == 2
    assert row["trades_scorable"] == 2
    assert row["trades_resolved"] == 1
    assert row["still_open"] == 1
    assert row["net_pnl_usd"] == Decimal("20.00")
    assert row["open_partial_pnl_usd"] == Decimal("35.00")
    assert row["avg_pnl_usd"] == Decimal("20.00")
    assert row["total_r"] == Decimal("2")
    assert row["avg_r_per_trade"] == Decimal("2.0000")
    assert row["avg_r_per_leg"] == Decimal("1.0000")
    assert row["benchmark_model"] == RETROSPECTIVE_BENCHMARK_MODEL
