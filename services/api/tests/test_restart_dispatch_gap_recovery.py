from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from app.production_listener import ProviderResearchProductionListener


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)


class _Session:
    def __init__(self, rows):
        self._rows = rows
        self.sql = ""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, statement):
        self.sql = str(statement)
        assert "ai_message_decisions" in self.sql
        assert "DISTINCT ON (d.message_id,d.revision_index)" in self.sql
        assert "source_status IN ('testing','shadow','live')" in self.sql
        return _RowsResult(self._rows)


class _Factory:
    def __init__(self, rows):
        self._rows = rows

    def __call__(self):
        return _Session(self._rows)


class _Inner:
    def __init__(self, rows):
        self._session_factory = _Factory(rows)
        self.calls: list[dict] = []

    async def _dispatch_recovered_if_required(self, **kwargs):
        self.calls.append(dict(kwargs))


def test_fresh_committed_decision_is_offered_to_router_after_restart() -> None:
    source_id = uuid4()
    occurred_at = datetime.now(UTC)
    inner = _Inner(
        [
            {
                "source_id": source_id,
                "telegram_message_id": 1386,
                "revision_index": 1,
                "occurred_at": occurred_at,
            }
        ]
    )
    listener = object.__new__(ProviderResearchProductionListener)
    listener._inner = inner

    checked = asyncio.run(listener._recover_committed_dispatch_gaps())

    assert checked == 1
    assert inner.calls == [
        {
            "source_id": source_id,
            "telegram_message_id": 1386,
            "revision_index": 1,
            "occurred_at": occurred_at,
        }
    ]


def test_empty_restart_sweep_is_a_noop() -> None:
    inner = _Inner([])
    listener = object.__new__(ProviderResearchProductionListener)
    listener._inner = inner

    checked = asyncio.run(listener._recover_committed_dispatch_gaps())

    assert checked == 0
    assert inner.calls == []
