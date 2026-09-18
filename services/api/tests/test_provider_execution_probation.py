"""A provider on probation only fires on its own historically-best side -- against real tables.

Same reason the other Decision Ledger integration suites use PostgreSQL directly: the
sample-floor gating and the "not listed = unaffected" default are meant to be exercised
against the real provider_trade_fingerprints/provider_execution_probation tables, not a mock.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app.provider_execution_probation import check_probation_eligibility, is_active_probation

DATABASE_URL = os.getenv("DATABASE_URL")
API_ROOT = Path(__file__).resolve().parents[1]


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
            "title": "Probation Test Group",
        },
    )
    return source_id


def add_fingerprint(
    conn,
    source_id: UUID,
    *,
    side_sample_met: bool,
    best_side: str | None,
    session_sample_met: bool = False,
) -> None:
    conn.execute(
        text(
            "INSERT INTO provider_trade_fingerprints "
            "(id,source_id,trades_resolved,wins,losses,geometry_sample_met,best_side,"
            "side_sample_met,session_sample_met,cohort_sample_met,summary) "
            "VALUES (:id,:source,20,12,8,false,:best_side,:side_met,:session_met,"
            ":side_met AND :session_met,'test summary')"
        ),
        {
            "id": uuid4(),
            "source": source_id,
            "best_side": best_side,
            "side_met": side_sample_met,
            "session_met": session_sample_met,
        },
    )


def test_a_provider_not_on_probation_is_completely_unaffected(conn, source_id) -> None:
    from sqlalchemy.orm import sessionmaker

    session_factory = sessionmaker(bind=conn, future=True, expire_on_commit=False)
    with session_factory() as session:
        result = check_probation_eligibility(session, source_id=source_id, side="BUY")

    assert result.eligible is True
    assert result.reason == "not_probationary"


def test_probation_matches_the_providers_own_best_side(conn, source_id) -> None:
    from sqlalchemy.orm import sessionmaker

    conn.execute(
        text("INSERT INTO provider_execution_probation (source_id) VALUES (:source)"),
        {"source": source_id},
    )
    add_fingerprint(conn, source_id, side_sample_met=True, best_side="BUY")

    session_factory = sessionmaker(bind=conn, future=True, expire_on_commit=False)
    with session_factory() as session:
        matching = check_probation_eligibility(session, source_id=source_id, side="BUY")
        opposite = check_probation_eligibility(session, source_id=source_id, side="SELL")

    assert matching.eligible is True
    assert matching.reason == "probation_matches_best_side"
    assert opposite.eligible is False
    assert "probation_side_not_historically_best" in opposite.reason


def test_probation_holds_back_when_evidence_is_still_too_thin(conn, source_id) -> None:
    from sqlalchemy.orm import sessionmaker

    conn.execute(
        text("INSERT INTO provider_execution_probation (source_id) VALUES (:source)"),
        {"source": source_id},
    )
    add_fingerprint(conn, source_id, side_sample_met=False, best_side=None)

    session_factory = sessionmaker(bind=conn, future=True, expire_on_commit=False)
    with session_factory() as session:
        result = check_probation_eligibility(session, source_id=source_id, side="BUY")

    assert result.eligible is False
    assert result.reason == "probation_insufficient_evidence"


def test_probation_holds_back_with_no_fingerprint_at_all_yet(conn, source_id) -> None:
    from sqlalchemy.orm import sessionmaker

    conn.execute(
        text("INSERT INTO provider_execution_probation (source_id) VALUES (:source)"),
        {"source": source_id},
    )

    session_factory = sessionmaker(bind=conn, future=True, expire_on_commit=False)
    with session_factory() as session:
        result = check_probation_eligibility(session, source_id=source_id, side="BUY")

    assert result.eligible is False
    assert result.reason == "probation_insufficient_evidence"


def test_a_graduated_provider_is_no_longer_restricted(conn, source_id) -> None:
    from sqlalchemy.orm import sessionmaker

    conn.execute(
        text(
            "INSERT INTO provider_execution_probation (source_id, graduated, graduated_at) "
            "VALUES (:source, true, now())"
        ),
        {"source": source_id},
    )
    add_fingerprint(conn, source_id, side_sample_met=True, best_side="BUY")

    session_factory = sessionmaker(bind=conn, future=True, expire_on_commit=False)
    with session_factory() as session:
        result = check_probation_eligibility(session, source_id=source_id, side="SELL")

    assert result.eligible is True
    assert result.reason == "probation_graduated"


def test_is_active_probation_true_only_while_listed_and_not_graduated(conn, source_id) -> None:
    from sqlalchemy.orm import sessionmaker

    session_factory = sessionmaker(bind=conn, future=True, expire_on_commit=False)
    with session_factory() as session:
        assert is_active_probation(session, source_id=source_id) is False

    conn.execute(
        text("INSERT INTO provider_execution_probation (source_id) VALUES (:source)"),
        {"source": source_id},
    )
    with session_factory() as session:
        assert is_active_probation(session, source_id=source_id) is True

    conn.execute(
        text(
            "UPDATE provider_execution_probation SET graduated=true, graduated_at=now() "
            "WHERE source_id=:source"
        ),
        {"source": source_id},
    )
    with session_factory() as session:
        assert is_active_probation(session, source_id=source_id) is False


def test_a_provider_with_thin_session_spread_but_solid_side_evidence_still_graduates(
    conn, source_id
) -> None:
    """Regression: GOLDHUNTER had 43 resolved trades with both BUY and SELL individually
    well past the cohort floor, but its trades clustered into one dominant session, so the
    fingerprint's session comparison could never be made. That used to zero out the combined
    ``cohort_sample_met`` flag this check read, holding every signal back forever even though
    the side evidence alone was fully sufficient. Side and session are independent axes now."""
    from sqlalchemy.orm import sessionmaker

    conn.execute(
        text("INSERT INTO provider_execution_probation (source_id) VALUES (:source)"),
        {"source": source_id},
    )
    add_fingerprint(
        conn, source_id, side_sample_met=True, session_sample_met=False, best_side="SELL"
    )

    session_factory = sessionmaker(bind=conn, future=True, expire_on_commit=False)
    with session_factory() as session:
        result = check_probation_eligibility(session, source_id=source_id, side="SELL")

    assert result.eligible is True
    assert result.reason == "probation_matches_best_side"


def test_unknown_signal_side_is_held_back_not_guessed(conn, source_id) -> None:
    from sqlalchemy.orm import sessionmaker

    conn.execute(
        text("INSERT INTO provider_execution_probation (source_id) VALUES (:source)"),
        {"source": source_id},
    )
    add_fingerprint(conn, source_id, side_sample_met=True, best_side="BUY")

    session_factory = sessionmaker(bind=conn, future=True, expire_on_commit=False)
    with session_factory() as session:
        result = check_probation_eligibility(session, source_id=source_id, side=None)

    assert result.eligible is False
    assert result.reason == "probation_signal_side_unknown"
