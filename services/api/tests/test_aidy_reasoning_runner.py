"""Selection scope, append-only persistence and budget gating against real tables.

Same reason test_aidy_decision_ledger.py and test_aidy_decision_outcome_runner.py use
PostgreSQL directly: the JSONB containment filter and the append-only trigger are SQL,
not something a mock can exercise honestly. The model call itself is faked -- this
suite never makes a real network call.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from app.aidy_context_client import AidyCanonicalContext, AidyContextTerminalMiss
from app.aidy_reasoning_engine import AidyReasoningUnavailable, ReasoningAnnotation, SignalContext
from app.aidy_reasoning_runner import AidyReasoningRunner
from app.provider_day19_explainer_budget import ResourceBudget

DATABASE_URL = os.getenv("DATABASE_URL")
API_ROOT = Path(__file__).resolve().parents[1]
OBSERVED_AT = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)

_WIDE_BUDGET = ResourceBudget(
    soft_d1_reads=0,
    hard_d1_reads=1,
    soft_metaapi_calls=0,
    hard_metaapi_calls=1,
    soft_openai_calls=1000,
    hard_openai_calls=2000,
    soft_cost_usd=1000.0,
    hard_cost_usd=2000.0,
)


@dataclass
class FakeEngine:
    """Returns a fixed annotation, or raises, without any real network call."""

    lean: str = "agree"
    should_fail: bool = False
    calls: list[SignalContext] | None = None

    def __post_init__(self) -> None:
        if self.calls is None:
            self.calls = []

    def reason(self, context: SignalContext) -> ReasoningAnnotation:
        self.calls.append(context)
        if self.should_fail:
            raise AidyReasoningUnavailable("forced_failure")
        return ReasoningAnnotation(
            lean=self.lean,
            confidence=0.65,
            rationale="test rationale",
            key_factors=["factor one"],
            model_name="fake-model",
            response_id="resp_fake",
            input_tokens=100,
            output_tokens=40,
            estimated_cost_usd=Decimal("0.001"),
            latency_ms=5,
        )


def _fake_context(*, as_of: datetime) -> AidyCanonicalContext:
    return AidyCanonicalContext(
        requested_as_of_utc=as_of,
        context_as_of_utc=as_of,
        context_lag_seconds=30,
        context_hash="hash",
        snapshot_id="snap",
        snapshot_digest="d" * 64,
        snapshot_archive_key="key",
        session={"computed_session_code": "london"},
        regime={
            "labels": {
                "session": "london",
                "trend_structure": "bullish_trend",
                "volatility_band": "normal",
                "event_timing": "clear_current_window",
            },
            "rule_evidence": {
                "trend_structure": {"directions": {"M15": "bullish", "H1": "bullish", "H4": "bullish"}}
            },
        },
        data_quality={"quote_freshness": "fresh", "quote_state": "known"},
        market={},
        provenance={"private_forward_only": True, "live_money_execution_allowed": False},
    )


@dataclass
class FakeMarketClient:
    """Async duck-type of AidyContextClient -- either returns a fixed context or raises."""

    fail_with: Exception | None = None
    requested: list[datetime] | None = None

    def __post_init__(self) -> None:
        if self.requested is None:
            self.requested = []

    async def fetch_context(self, *, as_of: datetime) -> AidyCanonicalContext:
        self.requested.append(as_of)
        if self.fail_with is not None:
            raise self.fail_with
        return _fake_context(as_of=as_of)


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
            "title": "Reasoning Test Group",
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
            "tg": 5000 + index,
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
            "'BUY','XAUUSD',2400,2401,2390,'[\"2410\",\"2420\"]','openai',:sha)"
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


def add_decision(
    conn, observation_id: UUID, source_id: UUID, *, decision_class: str, reasons: str
) -> UUID:
    decision_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO aidy_decisions (id,observation_id,source_id,signal_posted_at,decided_at,"
            "decision_class,reasons,model_version,rule_version,evidence_digest) "
            "VALUES (:id,:obs,:source,:posted,now(),:decision_class,CAST(:reasons AS jsonb),"
            "'m','r','d')"
        ),
        {
            "id": decision_id,
            "obs": observation_id,
            "source": source_id,
            "posted": OBSERVED_AT,
            "decision_class": decision_class,
            "reasons": reasons,
        },
    )
    return decision_id


_INSUFFICIENT_EVIDENCE = '[{"code": "insufficient_track_record_evidence", "trades_resolved": 3}]'
_TRACK_RECORD_OK = '[{"code": "provider_track_record_acceptable", "trades_resolved": 40}]'


def test_every_approve_decision_is_selected_regardless_of_track_record_reason(
    conn, source_id, session_factory
) -> None:
    thin_history_obs = add_observation(conn, source_id, index=1)
    add_decision(
        conn,
        thin_history_obs,
        source_id,
        decision_class="approve",
        reasons=_INSUFFICIENT_EVIDENCE,
    )
    established_obs = add_observation(conn, source_id, index=2)
    add_decision(
        conn, established_obs, source_id, decision_class="approve", reasons=_TRACK_RECORD_OK
    )
    denied_obs = add_observation(conn, source_id, index=3)
    add_decision(
        conn, denied_obs, source_id, decision_class="deny", reasons=_INSUFFICIENT_EVIDENCE
    )

    fake = FakeEngine()
    runner = AidyReasoningRunner(session_factory, engine=fake, budget=_WIDE_BUDGET)
    summary = asyncio.run(runner.run())

    assert summary.selected == 2, (
        "both approve decisions must be selected, not just the thin-history one"
    )
    assert summary.written == 2
    assert len(fake.calls) == 2
    assert all(call.side == "BUY" for call in fake.calls)
    assert "deny" not in [call.decision_class for call in fake.calls]


def test_a_decision_already_annotated_is_never_reselected(
    conn, source_id, session_factory
) -> None:
    observation_id = add_observation(conn, source_id, index=4)
    add_decision(
        conn, observation_id, source_id, decision_class="approve", reasons=_INSUFFICIENT_EVIDENCE
    )

    fake = FakeEngine()
    runner = AidyReasoningRunner(session_factory, engine=fake, budget=_WIDE_BUDGET)
    first = asyncio.run(runner.run())
    second = asyncio.run(runner.run())

    assert first.written == 1
    assert second.selected == 0, (
        "an annotation already exists -- this decision must not be re-asked"
    )


def test_a_failed_model_call_is_not_persisted_and_can_be_retried(
    conn, source_id, session_factory
) -> None:
    observation_id = add_observation(conn, source_id, index=5)
    add_decision(
        conn, observation_id, source_id, decision_class="approve", reasons=_INSUFFICIENT_EVIDENCE
    )

    failing = FakeEngine(should_fail=True)
    runner = AidyReasoningRunner(session_factory, engine=failing, budget=_WIDE_BUDGET)
    summary = asyncio.run(runner.run())

    assert summary.selected == 1
    assert summary.written == 0
    assert summary.failed == 1

    working = FakeEngine()
    retry_runner = AidyReasoningRunner(session_factory, engine=working, budget=_WIDE_BUDGET)
    retry_summary = asyncio.run(retry_runner.run())
    assert retry_summary.written == 1


def test_an_exhausted_budget_skips_the_pass_without_any_model_call(
    conn, source_id, session_factory
) -> None:
    # Simulate one call already made earlier this month, against a different decision.
    already_annotated_obs = add_observation(conn, source_id, index=60)
    already_annotated_decision = add_decision(
        conn,
        already_annotated_obs,
        source_id,
        decision_class="approve",
        reasons=_INSUFFICIENT_EVIDENCE,
    )
    conn.execute(
        text(
            "INSERT INTO aidy_reasoning_annotations "
            "(id,decision_id,lean,confidence,rationale,key_factors,model_version,"
            "prompt_version,model_name,input_tokens,output_tokens,estimated_cost_usd,latency_ms) "
            "VALUES (:id,:decision,'agree',0.5,'r','[]','m','p','model',1,1,0.001,1)"
        ),
        {"id": uuid4(), "decision": already_annotated_decision},
    )

    observation_id = add_observation(conn, source_id, index=6)
    add_decision(
        conn, observation_id, source_id, decision_class="approve", reasons=_INSUFFICIENT_EVIDENCE
    )

    already_exhausted_budget = ResourceBudget(
        soft_d1_reads=0,
        hard_d1_reads=1,
        soft_metaapi_calls=0,
        hard_metaapi_calls=1,
        soft_openai_calls=0,
        hard_openai_calls=1,
        soft_cost_usd=0.0,
        hard_cost_usd=1000.0,
    )
    fake = FakeEngine()
    runner = AidyReasoningRunner(session_factory, engine=fake, budget=already_exhausted_budget)
    summary = asyncio.run(runner.run())

    assert summary.skipped_budget is True
    assert summary.selected == 0
    assert len(fake.calls) == 0


def test_an_annotation_row_cannot_be_rewritten(conn, source_id) -> None:
    observation_id = add_observation(conn, source_id, index=7)
    decision_id = add_decision(
        conn, observation_id, source_id, decision_class="approve", reasons=_INSUFFICIENT_EVIDENCE
    )
    annotation_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO aidy_reasoning_annotations "
            "(id,decision_id,lean,confidence,rationale,key_factors,model_version,"
            "prompt_version,model_name,input_tokens,output_tokens,estimated_cost_usd,latency_ms) "
            "VALUES (:id,:decision,'agree',0.5,'r','[]','m','p','model',1,1,0.001,1)"
        ),
        {"id": annotation_id, "decision": decision_id},
    )

    with pytest.raises(DBAPIError), conn.begin_nested():
        conn.execute(
            text("UPDATE aidy_reasoning_annotations SET lean='disagree' WHERE id=:id"),
            {"id": annotation_id},
        )


def test_market_context_is_fetched_and_passed_to_the_engine_when_available(
    conn, source_id, session_factory
) -> None:
    observation_id = add_observation(conn, source_id, index=8)
    add_decision(
        conn, observation_id, source_id, decision_class="approve", reasons=_INSUFFICIENT_EVIDENCE
    )

    fake_engine = FakeEngine()
    market_client = FakeMarketClient()
    runner = AidyReasoningRunner(
        session_factory, engine=fake_engine, budget=_WIDE_BUDGET, market_client=market_client
    )
    summary = asyncio.run(runner.run())

    assert summary.written == 1
    assert market_client.requested == [OBSERVED_AT]
    assert fake_engine.calls[0].market_context is not None
    assert fake_engine.calls[0].market_context["trend_structure"] == "bullish_trend"
    assert fake_engine.calls[0].market_context["session"] == "london"

    row = conn.execute(
        text(
            "SELECT market_context_available FROM aidy_reasoning_annotations "
            "WHERE decision_id IN (SELECT id FROM aidy_decisions WHERE observation_id=:obs)"
        ),
        {"obs": observation_id},
    ).mappings().one()
    assert row["market_context_available"] is True


def test_a_stale_or_failed_market_context_lookup_never_blocks_reasoning(
    conn, source_id, session_factory
) -> None:
    observation_id = add_observation(conn, source_id, index=9)
    add_decision(
        conn, observation_id, source_id, decision_class="approve", reasons=_INSUFFICIENT_EVIDENCE
    )

    fake_engine = FakeEngine()
    stale_client = FakeMarketClient(fail_with=AidyContextTerminalMiss("pit_context_stale", payload={}))
    runner = AidyReasoningRunner(
        session_factory, engine=fake_engine, budget=_WIDE_BUDGET, market_client=stale_client
    )
    summary = asyncio.run(runner.run())

    assert summary.written == 1, "a stale context lookup must not stop the signal being reasoned"
    assert fake_engine.calls[0].market_context is None

    row = conn.execute(
        text(
            "SELECT market_context_available FROM aidy_reasoning_annotations "
            "WHERE decision_id IN (SELECT id FROM aidy_decisions WHERE observation_id=:obs)"
        ),
        {"obs": observation_id},
    ).mappings().one()
    assert row["market_context_available"] is False


def test_no_market_client_configured_reasons_exactly_as_before(
    conn, source_id, session_factory
) -> None:
    observation_id = add_observation(conn, source_id, index=10)
    add_decision(
        conn, observation_id, source_id, decision_class="approve", reasons=_INSUFFICIENT_EVIDENCE
    )

    fake_engine = FakeEngine()
    runner = AidyReasoningRunner(session_factory, engine=fake_engine, budget=_WIDE_BUDGET)
    summary = asyncio.run(runner.run())

    assert summary.written == 1
    assert fake_engine.calls[0].market_context is None
