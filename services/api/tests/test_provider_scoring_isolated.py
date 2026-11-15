"""Pure safety checks for the isolated monthly research scorer."""
from datetime import UTC, datetime

import pytest

from app.provider_scoring_isolated import (
    MonthlyProviderRunner, _MONTH_SELECT, month_window, psycopg_url,
)


def test_november_sofia_boundary_includes_dst_offset():
    start, end = month_window(datetime(2026, 11, 15, tzinfo=UTC))
    assert start == datetime(2026, 10, 31, 22, tzinfo=UTC)
    assert end == datetime(2026, 11, 30, 22, tzinfo=UTC)


def test_december_rollover():
    start, end = month_window(datetime(2026, 12, 15, tzinfo=UTC))
    assert start == datetime(2026, 11, 30, 22, tzinfo=UTC)
    assert end == datetime(2026, 12, 31, 22, tzinfo=UTC)


@pytest.mark.parametrize("url", [
    "postgresql://user@host/db",
    "postgres://user@host/db",
    "postgresql+psycopg://user@host/db",
])
def test_connection_url_psycopg(url):
    assert psycopg_url(url).startswith("postgresql+psycopg://")


def test_reject_wrong_db_dialect():
    with pytest.raises(ValueError):
        psycopg_url("sqlite:///:memory:")


def test_only_research_writes_and_no_duplicate_sources():
    sql = str(_MONTH_SELECT)
    assert "profile.duplicate_of_source_id IS NULL" in sql
    assert "Test Signal Provider" in sql
    assert "provider_trade_observations" in sql
    assert "provider_trade_scores" in sql
    assert "ORDER BY" in sql
    assert "row_number()" in sql
    assert "broker_deals" not in sql
    assert "positions" not in sql
    assert "order_send" not in sql


def test_limit_is_bounded():
    with pytest.raises(ValueError, match="between_1_and_100"):
        MonthlyProviderRunner._select(type("Fake", (), {})(), 1000)
