"""The Decision Ledger against real tables: exposure, duplication, and structural safety.

These use PostgreSQL directly because the selection rule, the conflict/duplicate
queries, and the append-only/research-only constraints are all SQL -- a mock cannot
tell us whether the CHECK constraints or the trigger actually fire.
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
from sqlalchemy.exc import DBAPIError

from app.aidy_decision_engine import AidyDecisionEngine
from app.aidy_decision_runner import AidyDecisionRunner, row_params

DATABASE_URL = os.getenv("DATABASE_URL")
API_ROOT = Path(__file__).resolve().parents[1]
OBSERVED_AT = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)  # always in the past vs decided_at=now()
_SELECT_OBSERVATION = (
    "SELECT id,source_id,observed_at,side,symbol FROM provider_trade_observations WHERE id=:id"
)


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
    """One rolled-back transaction per test; decisions are append-only."""
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
            "title": "Decision Ledger Test Group",
        },
    )
    return source_id


def add_observation(conn, source_id: UUID, *, side="BUY", index=0, observed_at=OBSERVED_AT) -> UUID:
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
    return observation_id


def test_a_provider_with_no_history_gets_a_decision_not_silence(
    conn, source_id, session_factory
) -> None:
    observation_id = add_observation(conn, source_id)
    engine = AidyDecisionEngine(session_factory)

    observation = conn.execute(
        text(_SELECT_OBSERVATION),
        {"id": observation_id},
    ).mappings().one()
    result = engine.evaluate(dict(observation))

    assert result.decision_class == "approve"
    assert result.reasons[0]["code"] == "insufficient_track_record_evidence"
    assert result.confidence is None


def test_a_second_same_direction_signal_within_the_window_is_held(
    conn, source_id, session_factory
) -> None:
    engine = AidyDecisionEngine(session_factory)

    first_id = add_observation(conn, source_id, side="BUY", index=1)
    first_obs = conn.execute(
        text(_SELECT_OBSERVATION),
        {"id": first_id},
    ).mappings().one()
    first_result = engine.evaluate(dict(first_obs))
    conn.execute(text(
        "INSERT INTO aidy_decisions (id,observation_id,source_id,signal_posted_at,decided_at,"
        "decision_class,reasons,confidence,model_version,rule_version,evidence_digest) VALUES "
        "(:id,:observation_id,:source_id,:signal_posted_at,:decided_at,:decision_class,"
        "CAST(:reasons AS jsonb),:confidence,:model_version,:rule_version,:evidence_digest)"
    ), row_params(first_result) | {"reasons": __import__("json").dumps(first_result.reasons)})

    second_id = add_observation(
        conn, source_id, side="BUY", index=2, observed_at=OBSERVED_AT + timedelta(minutes=5)
    )
    second_obs = conn.execute(
        text(_SELECT_OBSERVATION),
        {"id": second_id},
    ).mappings().one()
    second_result = engine.evaluate(dict(second_obs))

    assert second_result.decision_class == "hold_no_second_entry"
    assert second_result.duplicate_of_decision_id is not None
    assert second_result.reasons[0]["code"] == "duplicate_or_repost_within_window"


def test_a_signal_opposing_open_exposure_is_conflict_denied(
    conn, source_id, session_factory
) -> None:
    owner_id, message_id = uuid4(), uuid4()
    conn.execute(
        text("INSERT INTO users (id,email,status) VALUES (:id,:email,'active')"),
        {"id": owner_id, "email": f"{owner_id}@example.test"},
    )
    conn.execute(
        text(
            "INSERT INTO messages (id,source_id,telegram_message_id,raw_text,posted_at,"
            "ingestion_status,content_sha256,created_at) "
            "VALUES (:id,:source,9999,'BUY gold now',now(),'received',:sha,now())"
        ),
        {"id": message_id, "source": source_id, "sha": "9" * 64},
    )
    signal_id, position_id = uuid4(), uuid4()
    conn.execute(
        text(
            "INSERT INTO signals (id,source_message_id,symbol,side,original_text,source_id,"
            "provider_chat_id,provider_message_id,source_posted_at,signal_fingerprint) "
            "VALUES (:id,:message,'XAUUSD','BUY','BUY gold now',:source,9999,9999,now(),:fp)"
        ),
        {"id": signal_id, "message": message_id, "source": source_id, "fp": str(signal_id)},
    )
    conn.execute(
        text(
            "INSERT INTO positions (id,signal_id,user_id,tp_index,status,planned_risk_percent) "
            "VALUES (:id,:signal,:user,1,'open',1.0)"
        ),
        {"id": position_id, "signal": signal_id, "user": owner_id},
    )

    engine = AidyDecisionEngine(session_factory)
    observation_id = add_observation(conn, source_id, side="SELL", index=3)
    observation = conn.execute(
        text(_SELECT_OBSERVATION),
        {"id": observation_id},
    ).mappings().one()
    result = engine.evaluate(dict(observation))

    assert result.decision_class == "conflict_deny"
    assert result.reasons[0]["code"] == "opposite_direction_exposure_already_open"


def test_a_decision_cannot_claim_live_authority(conn, source_id) -> None:
    """The research boundary is structural, not documentary."""
    observation_id = add_observation(conn, source_id, index=4)

    with pytest.raises(DBAPIError), conn.begin_nested():
        conn.execute(
            text(
                "INSERT INTO aidy_decisions (id,observation_id,source_id,signal_posted_at,"
                "decided_at,decision_class,reasons,model_version,rule_version,evidence_digest,"
                "live_money_execution_allowed) "
                "VALUES (gen_random_uuid(),:obs,:source,now(),now(),'approve','[]','m','r',"
                "'d',true)"
            ),
            {"obs": observation_id, "source": source_id},
        )


def test_a_decision_cannot_be_rewritten(conn, source_id) -> None:
    observation_id = add_observation(conn, source_id, index=5)
    decision_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO aidy_decisions (id,observation_id,source_id,signal_posted_at,"
            "decided_at,decision_class,reasons,model_version,rule_version,evidence_digest) "
            "VALUES (:id,:obs,:source,now(),now(),'approve','[]','m','r','d')"
        ),
        {"id": decision_id, "obs": observation_id, "source": source_id},
    )

    with pytest.raises(DBAPIError), conn.begin_nested():
        conn.execute(
            text("UPDATE aidy_decisions SET decision_class='deny' WHERE id=:id"),
            {"id": decision_id},
        )


def test_runner_selects_only_new_and_thin_evidence_observations(
    conn, source_id, session_factory
) -> None:
    observation_id = add_observation(conn, source_id, index=6)
    runner = AidyDecisionRunner(session_factory)

    selected = runner._select(None)
    assert any(row["id"] == observation_id for row in selected)

    conn.execute(
        text(
            "INSERT INTO aidy_decisions (id,observation_id,source_id,signal_posted_at,"
            "decided_at,decision_class,reasons,model_version,rule_version,evidence_digest) "
            "VALUES (gen_random_uuid(),:obs,:source,now(),now(),'deny',"
            "'[{\"code\": \"provider_track_record_net_negative\"}]','m',:rule,'d')"
        ),
        {"obs": observation_id, "source": source_id, "rule": __import__(
            "app.aidy_decision_engine", fromlist=["RULE_VERSION"]
        ).RULE_VERSION},
    )

    selected_after = runner._select(None)
    assert not any(row["id"] == observation_id for row in selected_after), (
        "a resolved deny must not be relitigated just because it could be re-asked"
    )
