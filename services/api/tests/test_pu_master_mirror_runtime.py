"""PU mirror forward-only, idempotency and broker quote safety checks."""
from datetime import datetime, timedelta, timezone

from app.pu_master_mirror_runtime import _as_datetime, _mirror_client_id, _quote_recent


def test_unique_short_mt5_client_id_and_no_guess_for_non_tickets():
    assert _mirror_client_id("2234987676") == "SS_PU_2234987676"
    assert _mirror_client_id("abc") is None
    assert _mirror_client_id("1" * 19) is None


def test_quotes_reject_weekend_staleness_and_clock_future():
    now = datetime.now(timezone.utc)
    assert _quote_recent({"time": (now - timedelta(seconds=4)).isoformat()}, now)
    assert not _quote_recent({"time": (now - timedelta(minutes=3)).isoformat()}, now)
    assert not _quote_recent({"time": (now + timedelta(seconds=2)).isoformat()}, now)
    assert not _quote_recent({"time": None}, now)


def test_timestamp_parsing_for_source_cutoff():
    assert _as_datetime("2026-10-25T18:00:00Z").tzinfo is not None
    assert _as_datetime("invalid") is None


def test_pu_broker_three_hour_quote_offset_normalised():
    now = datetime.now(timezone.utc)
    assert _quote_recent({"time": (now + timedelta(hours=3) - timedelta(seconds=5)).isoformat()}, now)
    assert not _quote_recent({"time": (now + timedelta(hours=3) - timedelta(minutes=3)).isoformat()}, now)
    assert not _quote_recent({"time": (now + timedelta(hours=3, seconds=2)).isoformat()}, now)
