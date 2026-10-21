"""Public performance close-deal history: no trading writes, correct cash-day boundary."""
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import UUID

from app.routes.gold_quote import (
    PublicTradeDayResponse,
    _public_closed_broker_deals_for_day,
)

UID = UUID("00000000-0000-4000-8000-000000000001")


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rows):
        self.rows = rows
        self.statement = ""
        self.params = {}

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params):
        self.statement = str(sql)
        self.params = params
        return _Result(self.rows)


def test_broker_closed_deals_same_21_sofia_window_as_cash_ledger_and_no_identifiers():
    rows = [
        {
            "broker_deal_id": "mt5-real-private-ticket",
            "occurred_at": datetime(2026, 10, 21, 8, 0, tzinfo=UTC),
            "symbol": "XAUUSD",
            "deal_type": "DEAL_TYPE_SELL",
            "price": 4275.5,
            "volume": .02,
            "profit": "27.50",
            "commission": "-1.25",
            "swap": "-0.15",
        },
        {
            "broker_deal_id": "mt5-real-private-ticket-2",
            "occurred_at": datetime(2026, 10, 21, 9, 0, tzinfo=UTC),
            "symbol": "XAUUSD",
            "deal_type": "DEAL_TYPE_BUY",
            "price": 4278.2,
            "volume": .01,
            "profit": "-4.40",
            "commission": "-0.60",
            "swap": "0",
        },
    ]
    session = _Session(rows)
    service = SimpleNamespace(_session_factory=lambda: session)
    deals, truncated, net = _public_closed_broker_deals_for_day(
        service, UID, date(2026, 10, 21)
    )
    assert net == 21.10
    assert len(deals) == 2
    assert not truncated
    assert deals[0].net_cash_usd == 26.1
    assert deals[1].net_cash_usd == -5.
    assert deals[0].side == "SELL" and deals[1].side == "BUY"
    assert "mt5-real-private-ticket" not in str(deals)
    assert deals[0].receipt != deals[1].receipt
    assert session.params["start_at"].isoformat() == "2026-10-20T18:00:00+00:00"
    assert session.params["end_at"].isoformat() == "2026-10-21T18:00:00+00:00"
    assert "bd.entry_type='DEAL_ENTRY_OUT'" in session.statement
    assert "broker_deals" in session.statement and "LIMIT 501" in session.statement
    assert "performance_reporting_overrides" in session.statement
    assert "INSERT " not in session.statement and "UPDATE " not in session.statement
    assert "broker_client_id LIKE 'SS_%'" in session.statement


def test_future_and_old_client_schema_degrades_cleanly_and_caps_large_days():
    old = PublicTradeDayResponse(
        day=date(2026, 10, 21), trades=(), updated_at=datetime.now(UTC)
    )
    assert old.broker_closed_deals == ()
    assert old.broker_closed_deals_net_usd is None
    assert not old.broker_closed_deals_truncated
    first = {
        "broker_deal_id": "X",
        "occurred_at": datetime(2026, 10, 21, 8, tzinfo=UTC),
        "symbol": "XAUUSD",
        "deal_type": "BUY",
        "price": None, "volume": None,
        "profit": "1", "commission": "0", "swap": "0",
    }
    service = SimpleNamespace(_session_factory=lambda: _Session(
        [{**first, "broker_deal_id": str(i)} for i in range(501)]
    ))
    deals, truncated, net = _public_closed_broker_deals_for_day(
        service, UID, date(2026, 10, 21)
    )
    assert len(deals) == 500 and truncated and net == 500.
