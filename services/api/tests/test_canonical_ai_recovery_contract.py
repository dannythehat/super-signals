from uuid import uuid4

from app.canonical_signal_ledger import CanonicalSignalLedger
from app.production_ai_pipeline import ProductionAiMessagePipeline


class _Result:
    def __init__(self, *, row=None, scalar=None) -> None:
        self._row = row
        self._scalar = scalar

    def mappings(self):
        return self

    def first(self):
        return self._row

    def scalar_one(self):
        return self._scalar

    def scalar_one_or_none(self):
        return self._scalar


class _Session:
    def __init__(self) -> None:
        self.calls = []
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, params))
        if "FROM ai_message_decisions" in sql:
            return _Result(
                row={
                    "decision": "new_trade",
                    "action": "execute",
                    "decision_source": "deterministic_no_ai",
                }
            )
        if "SELECT EXISTS" in sql and "FROM positions" in sql:
            return _Result(scalar=True)
        if "SELECT id FROM signals" in sql:
            return _Result(scalar=uuid4())
        return _Result()

    def commit(self):
        self.commits += 1


class _Factory:
    def __init__(self, session: _Session) -> None:
        self.session = session

    def __call__(self):
        return self.session


def test_production_pipeline_owns_recovery_idempotency_helpers() -> None:
    assert "_existing_decision" in ProductionAiMessagePipeline.__dict__
    assert "_execution_started" in ProductionAiMessagePipeline.__dict__

    session = _Session()
    message_id = uuid4()
    signal_id = uuid4()

    row = ProductionAiMessagePipeline._existing_decision(session, message_id, 0)
    assert row == {
        "decision": "new_trade",
        "action": "execute",
        "decision_source": "deterministic_no_ai",
    }
    assert ProductionAiMessagePipeline._execution_started(session, signal_id) is True


def test_canonical_signal_ledger_owns_recovery_lookup_and_observation_helpers() -> None:
    assert "signal_id_for_message" in CanonicalSignalLedger.__dict__
    assert "record_observation" in CanonicalSignalLedger.__dict__

    session = _Session()
    ledger = CanonicalSignalLedger(_Factory(session))  # type: ignore[arg-type]
    message_id = uuid4()

    resolved = ledger.signal_id_for_message(message_id)
    assert resolved is not None

    # Use the real helper boundary but replace the low-level insert so this unit test
    # proves the public canonical contract without requiring a database fixture.
    observed = []
    ledger._observe = lambda *args, **kwargs: observed.append(kwargs)  # type: ignore[method-assign]
    ledger.record_observation(
        signal_id=uuid4(),
        message_id=message_id,
        revision_index=2,
        disposition="post_execution_edit",
        fingerprint=None,
    )
    assert observed[0]["revision_index"] == 2
    assert observed[0]["disposition"] == "post_execution_edit"
    assert session.commits == 1
