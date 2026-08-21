from datetime import UTC, datetime, timedelta

from app.canonical_signal_ledger import _duplicate_window


def test_duplicate_window_covers_repost_after_earlier_message() -> None:
    posted_at = datetime(2026, 8, 21, 11, 1, 45, tzinfo=UTC)
    start, end = _duplicate_window(posted_at)

    assert start == posted_at - timedelta(seconds=15)
    assert end == posted_at + timedelta(seconds=15)
    # Production incident: corrected repost 61509 arrived 10 seconds after 61508.
    assert start <= posted_at + timedelta(seconds=10) <= end


def test_duplicate_window_excludes_genuinely_later_identical_setup() -> None:
    posted_at = datetime(2026, 8, 21, 11, 1, 45, tzinfo=UTC)
    start, end = _duplicate_window(posted_at)

    assert not (start <= posted_at + timedelta(seconds=16) <= end)
