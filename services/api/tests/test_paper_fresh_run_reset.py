from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.paper_fresh_run_reset import (
    _clamp_today_session_start,
    _same_reset_day,
    _zero_window,
    paper_reset_at,
)


def test_paper_reset_at_accepts_utc_z(monkeypatch) -> None:
    monkeypatch.setenv("SUPER_SIGNALS_PAPER_RESET_AT", "2026-08-19T06:00:00Z")
    assert paper_reset_at() == datetime(2026, 8, 19, 6, 0, tzinfo=UTC)


def test_paper_reset_at_requires_valid_timestamp(monkeypatch) -> None:
    monkeypatch.setenv("SUPER_SIGNALS_PAPER_RESET_AT", "not-a-time")
    assert paper_reset_at() is None


def test_paper_reset_at_normalizes_naive_timestamp_to_utc(monkeypatch) -> None:
    monkeypatch.setenv("SUPER_SIGNALS_PAPER_RESET_AT", "2026-08-19T06:00:00")
    assert paper_reset_at() == datetime(2026, 8, 19, 6, 0, tzinfo=UTC)


def test_reset_day_historical_window_is_explicit_zero() -> None:
    window = _zero_window("7d", "7 days")
    assert window.cash_pnl == 0
    assert window.closed_trades == 0
    assert window.wins == 0
    assert window.losses == 0
    assert window.breakeven == 0
    assert window.open_trades == 0


def test_reset_day_detection_uses_utc_boundary() -> None:
    cutoff = datetime(2026, 8, 19, 6, 0, tzinfo=UTC)
    assert _same_reset_day(datetime(2026, 8, 19, 23, 59, tzinfo=UTC), cutoff)
    assert not _same_reset_day(datetime(2026, 8, 20, 0, 0, tzinfo=UTC), cutoff)


def test_today_session_is_clamped_to_owner_reset_epoch_only() -> None:
    owner = uuid4()
    other = uuid4()
    day_start = datetime(2026, 8, 18, 21, 0, tzinfo=UTC)  # midnight Europe/Sofia
    day_end = day_start + timedelta(days=1)
    cutoff = datetime(2026, 8, 19, 6, 0, tzinfo=UTC)  # 09:00 Europe/Sofia

    assert _clamp_today_session_start(
        day_start,
        user_id=owner,
        owner_id=owner,
        cutoff=cutoff,
        day_start=day_start,
        day_end=day_end,
    ) == cutoff
    assert _clamp_today_session_start(
        day_start,
        user_id=other,
        owner_id=owner,
        cutoff=cutoff,
        day_start=day_start,
        day_end=day_end,
    ) == day_start


def test_today_session_does_not_reapply_old_reset_on_future_days() -> None:
    owner = uuid4()
    cutoff = datetime(2026, 8, 19, 6, 0, tzinfo=UTC)
    next_day_start = datetime(2026, 8, 19, 21, 0, tzinfo=UTC)
    assert _clamp_today_session_start(
        next_day_start,
        user_id=owner,
        owner_id=owner,
        cutoff=cutoff,
        day_start=next_day_start,
        day_end=next_day_start + timedelta(days=1),
    ) == next_day_start
