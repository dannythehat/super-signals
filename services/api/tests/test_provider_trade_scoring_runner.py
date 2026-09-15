"""A score is derived, so it has to be allowed to change when the history behind it does.

The days AIDY's capture was down are the reason most trades cannot be scored today. Once
those minutes are backfilled the same trade has an answer, so the runner has to re-ask
exactly the rows whose answer can still change and leave settled ones alone -- otherwise
a backfill improves nothing, or every run rewrites the whole table.

These tests use the real SQL against PostgreSQL rather than a stub, because the
selection rule is the SQL.
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
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.provider_trade_scorer import TradeScore
from app.provider_trade_scoring_runner import (
    ProviderTradeScoringRunner,
    ScoringSummary,
    _row_params,
)

DATABASE_URL = os.getenv("DATABASE_URL")
API_ROOT = Path(__file__).resolve().parents[1]
OBSERVED_AT = datetime(2026, 9, 2, 10, 0, tzinfo=UTC)


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
    """One rolled-back transaction per test; observations are append-only."""
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
            "VALUES (:id,:account,:chat_id,:title,:title,'shadow',now(),now())"
        ),
        {
            "id": source_id,
            "account": account_id,
            "chat_id": -abs(hash(str(source_id))) % 10**12,
            "title": "Scoring Test Group",
        },
    )
    return source_id


def add_observation(conn, source_id: UUID, *, stop_loss="3990", take_profits='["4010"]',
                    side="BUY", index=0) -> UUID:
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
            "tg": 1000 + index,
            "posted": OBSERVED_AT,
            "sha": f"{index:064d}",
        },
    )
    conn.execute(
        text(
            "INSERT INTO provider_trade_observations "
            "(id,message_id,source_id,revision_index,observed_at,decision,action,executable,"
            "outcome_reason,side,entry_low,entry_high,stop_loss,take_profits,"
            "decision_source,raw_text_sha256) "
            "VALUES (:id,:message,:source,0,:observed,'new_trade','skip',false,'missing_sl',"
            ":side,4000,4000,:stop,CAST(:tps AS jsonb),'openai',:sha)"
        ),
        {
            "id": observation_id,
            "message": message_id,
            "source": source_id,
            "observed": OBSERVED_AT,
            "side": side,
            "stop": stop_loss,
            "tps": take_profits,
            "sha": f"{index:064d}",
        },
    )
    return observation_id


def add_score(conn, observation_id: UUID, source_id: UUID, **overrides) -> None:
    score = TradeScore(
        observation_id=observation_id,
        source_id=source_id,
        outcome=overrides.get("outcome", "won"),
        unresolvable_reason=overrides.get("unresolvable_reason"),
        entry_convention="zone",
        entry_price=Decimal("4000"),
        net_pnl_usd=overrides.get("net_pnl_usd", Decimal("10.00")),
        realized_r=Decimal("1"),
        legs_resolved=1,
        legs_total=1,
        first_bar_utc=OBSERVED_AT,
        last_bar_utc=OBSERVED_AT + timedelta(minutes=5),
        bars_replayed=5,
        missing_minutes=0,
    )
    from app.provider_trade_scoring_runner import _UPSERT

    conn.execute(text(_UPSERT), _row_params(score))


def selected(conn, source_id: UUID) -> set[UUID]:
    from app.provider_trade_scoring_runner import _SELECTABLE

    rows = conn.execute(text(_SELECTABLE)).mappings().all()
    return {UUID(str(row["id"])) for row in rows if row["source_id"] == source_id}


def test_an_unscored_trade_is_selected(conn, source_id) -> None:
    observation_id = add_observation(conn, source_id)

    assert observation_id in selected(conn, source_id)


def test_a_trade_that_could_not_be_scored_for_want_of_history_is_reselected(
    conn, source_id
) -> None:
    """This is the whole point: a backfill has to give these trades an answer."""
    observation_id = add_observation(conn, source_id)
    add_score(
        conn,
        observation_id,
        source_id,
        outcome="unresolvable",
        unresolvable_reason="no_price_history_for_window",
        net_pnl_usd=None,
    )

    assert observation_id in selected(conn, source_id)


def test_a_trade_still_running_at_the_window_end_is_reselected(conn, source_id) -> None:
    observation_id = add_observation(conn, source_id)
    add_score(conn, observation_id, source_id, outcome="open_at_window_end")

    assert observation_id in selected(conn, source_id)


@pytest.mark.parametrize("outcome", ["won", "lost", "breakeven", "never_entered"])
def test_a_settled_trade_is_not_rescored(conn, source_id, outcome) -> None:
    """More bars cannot change a trade that already hit its stop or its targets."""
    observation_id = add_observation(conn, source_id)
    add_score(
        conn,
        observation_id,
        source_id,
        outcome=outcome,
        net_pnl_usd=None if outcome == "never_entered" else Decimal("10.00"),
    )

    assert observation_id not in selected(conn, source_id)


def test_a_trade_the_interpreter_could_not_complete_is_not_selected(conn, source_id) -> None:
    """A trade with no stop is reported as unmirrorable elsewhere; scoring it is noise."""
    observation_id = add_observation(conn, source_id, stop_loss=None)

    assert observation_id not in selected(conn, source_id)


def test_rescoring_replaces_the_figure_rather_than_duplicating_it(conn, source_id) -> None:
    observation_id = add_observation(conn, source_id)
    add_score(
        conn,
        observation_id,
        source_id,
        outcome="unresolvable",
        unresolvable_reason="no_price_history_for_window",
        net_pnl_usd=None,
    )
    add_score(conn, observation_id, source_id, outcome="lost", net_pnl_usd=Decimal("-10.00"))

    rows = conn.execute(
        text("SELECT outcome,net_pnl_usd,unresolvable_reason FROM provider_trade_scores "
             "WHERE observation_id=:id"),
        {"id": observation_id},
    ).mappings().all()
    assert len(rows) == 1
    assert rows[0]["outcome"] == "lost"
    assert rows[0]["unresolvable_reason"] is None


def test_a_score_cannot_become_forward_evidence(conn, source_id) -> None:
    """The research boundary is structural, not documentary."""
    observation_id = add_observation(conn, source_id)

    with pytest.raises(DBAPIError), conn.begin_nested():
        conn.execute(
            text(
                "INSERT INTO provider_trade_scores "
                "(id,observation_id,source_id,benchmark_model,entry_convention,outcome,"
                "forward_evidence_eligible) "
                "VALUES (gen_random_uuid(),:o,:s,'m','zone','won',true)"
            ),
            {"o": observation_id, "s": source_id},
        )


def test_an_unresolvable_score_cannot_carry_a_profit(conn, source_id) -> None:
    """A figure and an excuse are mutually exclusive shapes."""
    observation_id = add_observation(conn, source_id)

    with pytest.raises(DBAPIError), conn.begin_nested():
        conn.execute(
            text(
                "INSERT INTO provider_trade_scores "
                "(id,observation_id,source_id,benchmark_model,entry_convention,outcome,"
                "unresolvable_reason,net_pnl_usd) "
                "VALUES (gen_random_uuid(),:o,:s,'m','zone','unresolvable','no_history',12.5)"
            ),
            {"o": observation_id, "s": source_id},
        )


def test_the_scoreboard_reports_coverage_and_the_management_caveat(conn, source_id) -> None:
    """A P&L without its coverage invites being read as the whole picture."""
    scored = add_observation(conn, source_id, index=1)
    add_observation(conn, source_id, index=2)
    add_score(conn, scored, source_id, outcome="won", net_pnl_usd=Decimal("10.00"))

    row = conn.execute(
        text("SELECT * FROM provider_trade_scoreboard WHERE source_id=:id"),
        {"id": source_id},
    ).mappings().one()
    assert row["trades_scorable"] == 2
    assert row["trades_resolved"] == 1
    assert row["wins"] == 1
    assert row["net_pnl_usd"] == Decimal("10.00")
    assert row["win_rate_pct"] == Decimal("100.0")
    assert row["scored_coverage_pct"] == Decimal("50.0")


def test_a_provider_with_no_scores_yet_still_appears(conn, source_id) -> None:
    """Absence of a figure has to be visible, not an absent row."""
    add_observation(conn, source_id)

    row = conn.execute(
        text("SELECT * FROM provider_trade_scoreboard WHERE source_id=:id"),
        {"id": source_id},
    ).mappings().one()
    assert row["trades_recorded"] == 1
    assert row["trades_resolved"] == 0
    assert row["win_rate_pct"] is None


class StubClient:
    async def fetch_research_m1(self, *, start, end):  # pragma: no cover - unused
        raise AssertionError("the runner under test must not reach the market")


def test_the_summary_counts_what_it_wrote() -> None:
    summary = ScoringSummary(selected=2)
    summary.record(
        TradeScore(uuid4(), uuid4(), "won", None, "zone", Decimal("4000"),
                   Decimal("10"), Decimal("1"), 1, 1, None, None, 5, 0)
    )
    summary.record(
        TradeScore(uuid4(), uuid4(), "unresolvable", "no_price_history_for_window",
                   "zone", None, None, None, 0, 1, None, None, 0, 0)
    )

    assert summary.written == 2
    assert summary.outcomes == {"won": 1, "unresolvable": 1}
    assert summary.reasons == {"no_price_history_for_window": 1}


def test_concurrency_must_be_positive() -> None:
    with pytest.raises(ValueError, match="concurrency"):
        ProviderTradeScoringRunner(sessionmaker(), StubClient(), concurrency=0)
