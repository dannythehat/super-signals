from datetime import UTC, datetime
from uuid import UUID

from app.ai_lifecycle_bridge import AiLifecycleBridge


class _MappingsResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)


class _LifecycleSession:
    def __init__(self, active_rows, standalone_rows):
        self.active_rows = active_rows
        self.standalone_rows = standalone_rows
        self.calls = []

    def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append(sql)
        if "JOIN positions AS p" in sql:
            return _MappingsResult(self.active_rows)
        if "NOT EXISTS" in sql and "FROM signals AS s" in sql:
            return _MappingsResult(self.standalone_rows)
        raise AssertionError(f"unexpected_sql: {sql}")


def _candidate(value: int, provider_message_id: int, minute: int):
    return {
        "id": UUID(int=value),
        "symbol": "XAUUSD",
        "provider_message_id": provider_message_id,
        "source_posted_at": datetime(2026, 9, 1, 14, minute, tzinfo=UTC),
    }


def _row(raw_text: str):
    return {
        "message_id": UUID(int=900),
        "source_id": UUID(int=901),
        "telegram_message_id": 84095,
        "raw_text": raw_text,
        "raw_payload": {},
        "occurred_at": datetime(2026, 9, 1, 14, 12, 56, tzinfo=UTC),
    }


def test_fxt_tp_management_can_use_unique_active_standalone_context() -> None:
    older = _candidate(1, 84043, 0)
    newest = _candidate(2, 84094, 10)
    session = _LifecycleSession([newest, older], [newest])

    linked, reason = AiLifecycleBridge._resolve_signal(
        session,
        _row(
            "TP 1 & 2 are BOTH hit ✅\n\n"
            "+ 90 pips profit secured.\n\n"
            "Move your SL back to entry."
        ),
        revision_index=0,
    )

    assert linked is not None
    assert linked["id"] == newest["id"]
    assert reason == "standalone_unique_active"
    assert len(session.calls) == 2


def test_bare_ambiguous_breakeven_still_fails_closed() -> None:
    older = _candidate(1, 84043, 0)
    newest = _candidate(2, 84094, 10)
    session = _LifecycleSession([newest, older], [newest])

    linked, reason = AiLifecycleBridge._resolve_signal(
        session,
        _row("BE now"),
        revision_index=0,
    )

    assert linked is None
    assert reason == "active_trade_target_ambiguous"
    assert len(session.calls) == 1
