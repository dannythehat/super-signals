from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.paper_critical_publication_override import (
    _PAPER_CRITICAL_PUBLICATION_CUTOVER,
    _seed_paper_critical_publications,
)
from app.telegram_publisher_day34_cutover import Day34CutoverTelegramPublisherManager


class _ScalarResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one(self) -> Any:
        return self._value


class _FakeSession:
    def __init__(self) -> None:
        self.executed: list[tuple[str, dict[str, Any] | None]] = []
        self.committed = False

    def __enter__(self) -> "_FakeSession":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        sql = str(statement)
        self.executed.append((sql, params))
        if "SELECT publish_after FROM day34_summary_state" in sql:
            return _ScalarResult(datetime(2026, 8, 1, tzinfo=UTC))
        return _ScalarResult(None)

    def commit(self) -> None:
        self.committed = True


class _FakeManager:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session

    def _session_factory(self) -> _FakeSession:
        return self.session


def test_paper_critical_publication_bridge_is_installed_at_package_import() -> None:
    method = Day34CutoverTelegramPublisherManager._seed_missing_publications
    assert getattr(method, "_paper_critical_publication_support", False) is True
    assert getattr(method, "_paper_critical_publication_cutover", None) == (
        _PAPER_CRITICAL_PUBLICATION_CUTOVER
    )


def test_paper_critical_bridge_is_forward_only_from_aug19_bulgaria_midnight() -> None:
    assert _PAPER_CRITICAL_PUBLICATION_CUTOVER == datetime(2026, 8, 18, 21, 0, tzinfo=UTC)


def test_paper_critical_bridge_seeds_root_lifecycle_and_member_notifications() -> None:
    session = _FakeSession()
    manager = _FakeManager(session)

    _seed_paper_critical_publications(manager)

    sql = "\n".join(statement for statement, _ in session.executed)
    assert sql.count("mt5.paper_critical_execution_success") == 5
    assert "INSERT INTO telegram_publications" in sql
    assert "'signal_created', 'pending'" in sql
    assert "'lifecycle_event', 'pending'" in sql
    assert "INSERT INTO notification_events" in sql
    assert session.committed is True

    for statement, params in session.executed:
        if "SELECT publish_after FROM day34_summary_state" in statement:
            continue
        assert params is not None
        assert params["paper_critical_publish_after"] == _PAPER_CRITICAL_PUBLICATION_CUTOVER
