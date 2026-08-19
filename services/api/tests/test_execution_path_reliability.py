from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from app.critical_entry_policy import CriticalEntry
from app.paper_critical_execution import _Planned
from app.paper_fresh_start_execution import PaperFreshStartExecutionService


class _Session:
    def __init__(self) -> None:
        self.statement = ""
        self.params = None
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, statement, params):
        self.statement = str(statement)
        self.params = dict(params)

    def commit(self) -> None:
        self.committed = True


def test_critical_order_persistence_uses_boolean_open_bind_not_status_twice() -> None:
    """Regress the live PostgreSQL text-vs-varchar AmbiguousParameter failure."""
    session = _Session()
    service = object.__new__(PaperFreshStartExecutionService)
    service._session_factory = lambda: session
    item = _Planned(
        local_id=UUID("10000000-0000-4000-8000-000000000001"),
        entry=CriticalEntry(1, "market", Decimal("4391")),
        tp_index=1,
        take_profit=Decimal("4393"),
        client_id="SS_TEST",
        sizing=None,  # type: ignore[arg-type]
    )

    service._record_critical_order(item, "1787000000", "1787000000")

    assert session.committed is True
    assert session.params is not None
    assert session.params["status"] == "open"
    assert session.params["is_open"] is True
    assert "CASE WHEN :is_open" in session.statement
    assert "CASE WHEN :status='open'" not in session.statement
