from datetime import UTC, datetime

from app.paper_fresh_run_reset import paper_reset_at


def test_paper_reset_at_accepts_utc_z(monkeypatch) -> None:
    monkeypatch.setenv("SUPER_SIGNALS_PAPER_RESET_AT", "2026-08-18T06:00:00Z")
    assert paper_reset_at() == datetime(2026, 8, 18, 6, 0, tzinfo=UTC)


def test_paper_reset_at_requires_valid_timestamp(monkeypatch) -> None:
    monkeypatch.setenv("SUPER_SIGNALS_PAPER_RESET_AT", "not-a-time")
    assert paper_reset_at() is None


def test_paper_reset_at_normalizes_naive_timestamp_to_utc(monkeypatch) -> None:
    monkeypatch.setenv("SUPER_SIGNALS_PAPER_RESET_AT", "2026-08-18T06:00:00")
    assert paper_reset_at() == datetime(2026, 8, 18, 6, 0, tzinfo=UTC)
