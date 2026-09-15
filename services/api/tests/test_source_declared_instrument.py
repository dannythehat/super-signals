"""A source declares its instrument in data, not in a hardcoded list.

Which sources could have a missing instrument filled in was decided by a five-entry set
of profile ids fed by a nine-entry dictionary of chat titles. Neither grew with the
business, so of 33 connected groups only the original handful qualified and GOLDHUNTER's
complete signals -- "Buy at 4392.63 SL 4377.63 4403 4413 4423" -- were refused for not
containing the word gold.

The declaration is owner-set and never inferred from a title, because titles are not
evidence: "FOREX GOLD XAUUSD SIGNALS" posts "NEW #USOIL SELL SIGNAL". And a declaration
only fills a silence; it can never override an instrument the provider actually named.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app.ai_message_pipeline_canonical import CanonicalAiMessagePipeline
from app.ai_message_supervisor import AiMessageDecision
from app.v1_message_policy import DECLARED_XAUUSD_PROFILE, apply_v1_message_policy

DATABASE_URL = os.getenv("DATABASE_URL")
API_ROOT = Path(__file__).resolve().parents[1]

GOLDHUNTER_SIGNAL = "Buy at 4392.63 SL 4377.63 4403 4413 4423"


def _decision(**overrides) -> AiMessageDecision:
    extracted = {
        "side": "BUY",
        "symbol": "XAUUSD",
        "entry_low": "4392.63",
        "entry_high": "4392.63",
        "stop_loss": "4377.63",
        "take_profits": ["4403", "4413", "4423"],
        "order_type": "market",
        "source_profile": DECLARED_XAUUSD_PROFILE,
    }
    extracted.update(overrides.pop("extracted", {}))
    base = {
        "decision": "new_trade",
        "action": "execute",
        "confidence": 0.8,
        "reason": "candidate",
        "extracted": extracted,
        "model": "gpt-5-mini-2025-08-07",
        "response_id": None,
        "latency_ms": 5,
        "source": "openai",
        "raw_text_sha256": "a" * 64,
    }
    base.update(overrides)
    return AiMessageDecision(**base)


def test_declared_source_may_supply_the_missing_gold_identity() -> None:
    """GOLDHUNTER's signal: complete, and previously refused for not saying "gold"."""
    result = apply_v1_message_policy(_decision(), raw_text=GOLDHUNTER_SIGNAL)

    assert result.reason != "missing_instrument"
    assert result.action == "execute"


def test_undeclared_source_is_still_refused() -> None:
    """The declaration is what enables this; absence of one must still refuse."""
    result = apply_v1_message_policy(
        _decision(extracted={"source_profile": None}), raw_text=GOLDHUNTER_SIGNAL
    )

    assert result.reason == "missing_instrument"


def test_a_declaration_cannot_override_a_named_foreign_instrument() -> None:
    """The property that makes declaring an instrument safe at all."""
    result = apply_v1_message_policy(
        _decision(
            extracted={
                "side": "SELL",
                "entry_low": "79794.4",
                "entry_high": "79794.4",
                "stop_loss": "80994.4",
                "take_profits": ["76194.4"],
            }
        ),
        raw_text="BTCUSD SELL 79794.4 SL: 80994.4 TP: 76194.4",
    )

    assert result.action == "skip"
    assert result.reason == "foreign_instrument"


def test_declaration_does_not_donate_prices() -> None:
    """A profile supplies an instrument identity and nothing else."""
    result = apply_v1_message_policy(
        _decision(extracted={"stop_loss": None, "take_profits": []}),
        raw_text="Buy at 4392.63",
    )

    assert result.action == "skip"
    assert result.reason in {"missing_sl", "missing_tp"}


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


def _seed_source(conn, *, title: str, declared: str | None):
    user_id, account_id, source_id = uuid4(), uuid4(), uuid4()
    conn.execute(
        text("INSERT INTO users (id,email,status) VALUES (:id,:email,'active')"),
        {"id": user_id, "email": f"{user_id}@example.test"},
    )
    conn.execute(
        text(
            "INSERT INTO telegram_accounts "
            "(id,owner_user_id,label,phone_number_e164,session_ciphertext,session_fingerprint) "
            "VALUES (:id,:owner,'t',:phone,:cipher,:fp)"
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
            "INSERT INTO sources (id,telegram_account_id,chat_id,chat_title,source_alias,"
            "status,declared_instrument) VALUES (:id,:a,:c,:t,:t,'shadow',:d)"
        ),
        {
            "id": source_id,
            "a": account_id,
            "c": -abs(hash(str(source_id))) % 10**12,
            "t": title,
            "d": declared,
        },
    )
    return source_id


def _resolve(conn, source_id):
    pipeline = CanonicalAiMessagePipeline.__new__(CanonicalAiMessagePipeline)

    class _Factory:
        def __call__(self):
            return self

        def __enter__(self):
            return conn

        def __exit__(self, *exc):
            return False

    pipeline._session_factory = _Factory()
    return pipeline._source_profile(source_id)


def test_declared_source_resolves_to_the_declared_profile(engine) -> None:
    connection = engine.connect()
    transaction = connection.begin()
    try:
        source_id = _seed_source(connection, title="GOLDHUNTER | PAUL", declared="XAUUSD")
        assert _resolve(connection, source_id) == DECLARED_XAUUSD_PROFILE
    finally:
        transaction.rollback()
        connection.close()


def test_undeclared_source_resolves_to_no_profile(engine) -> None:
    connection = engine.connect()
    transaction = connection.begin()
    try:
        source_id = _seed_source(connection, title="Some New Group", declared=None)
        assert _resolve(connection, source_id) is None
    finally:
        transaction.rollback()
        connection.close()


def test_handwritten_profile_keeps_priority_over_a_declaration(engine) -> None:
    """A hand-written profile carries grammar knowledge, so it must still win."""
    connection = engine.connect()
    transaction = connection.begin()
    try:
        source_id = _seed_source(connection, title="SureShot GOLD", declared="XAUUSD")
        assert _resolve(connection, source_id) == "sureshot_xauusd"
    finally:
        transaction.rollback()
        connection.close()


def test_existing_gold_sources_are_seeded_by_the_migration(engine) -> None:
    """Seeding records in data what a hardcoded set previously implied."""
    with engine.connect() as connection:
        declared = connection.execute(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name='sources' AND column_name='declared_instrument'"
            )
        ).scalar_one()
    assert declared == 1
