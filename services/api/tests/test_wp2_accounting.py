"""Posted cash, account isolation and partial-volume regressions; no broker calls."""

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from uuid import uuid4
import pytest
from app.performance_ledger_day33 import (
    Day33PerformanceLedgerService,
    _broker_deal_cash,
    _broker_deal_fee,
)
from app.performance_ledger_canonical import CanonicalPerformanceLedgerService
from app.running_daily_balance import RunningAccountDay

NOW = datetime(2026, 11, 6, 0, tzinfo=UTC)
ACCOUNT = uuid4()
USER = uuid4()


class Result:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class Session:
    def __init__(self, snapshots, deals):
        self.responses = [snapshots, deals]
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params):
        assert str(sql).lstrip().startswith("SELECT")
        self.calls.append((str(sql), params))
        return Result(self.responses.pop(0))


def reconcile(deals, identities=None):
    snapshots = [
        dict(mt5_account_id=ACCOUNT, currency="USC", balance="10000", captured_at=NOW),
        dict(
            mt5_account_id=ACCOUNT,
            currency="USC",
            balance="10844",
            captured_at=NOW + timedelta(hours=1),
        ),
    ]
    if identities:
        snapshots = identities
    session = Session(snapshots, deals)
    service = object.__new__(CanonicalPerformanceLedgerService)
    service._session_factory = lambda: session
    return service.read_account_reconciliation(
        USER, start_time=NOW, end_time=NOW + timedelta(days=1), mt5_account_id=ACCOUNT
    ), session


def cash(typ, profit="0", commission="0", swap="0", fee="0"):
    return dict(
        deal_type=typ,
        profit=D(profit),
        commission=D(commission),
        swap=D(swap),
        raw_payload={"fee": fee},
    )


def test_deposits_withdrawals_trade_costs_and_cent_native_units():
    r, s = reconcile(
        [
            cash("DEAL_TYPE_BALANCE", "1000"),
            cash("DEAL_TYPE_BALANCE", "-200"),
            cash("DEAL_TYPE_BUY", "50", "-2", "-3", "-1"),
        ]
    )
    assert r["cash_movement"] == 800 and r["trading_cash"] == 44
    assert r["currency"] == "USC" and r["reconciliation_gap"] == 0 and r["reconciled"]
    assert r["charges_complete"]
    assert s.calls[1][1]["account_id"] == ACCOUNT
    assert "mt5_account_id=:account_id" in s.calls[1][0]


def test_credit_unknown_types_and_missing_fees_are_not_invented():
    deal = cash("DEAL_TYPE_BUY", "44")
    deal["raw_payload"] = {}
    r, _ = reconcile(
        [
            cash("DEAL_TYPE_BALANCE", "800"),
            deal,
            cash("DEAL_TYPE_CREDIT", "2"),
            cash("DEAL_TYPE_CORRECTION", "-2"),
        ]
    )
    assert r["trading_cash"] == 44 and r["credit_movement"] == 2 and r["unclassified_cash"] == -2
    assert not r["evidence_complete"] and r["missing_fee_evidence"] == 1
    assert _broker_deal_fee({"raw_payload": {"fee": "NaN"}}) is None
    assert _broker_deal_fee({"raw_payload": {"fee": False}}) is None
    assert _broker_deal_cash({"profit": "5", "commission": None, "swap": None}) == 5


@pytest.mark.parametrize("field,value", [("mt5_account_id", uuid4()), ("currency", "USD")])
def test_multiple_account_or_currency_is_not_silently_pooled(field, value):
    rows = [
        dict(mt5_account_id=ACCOUNT, currency="USC", balance=1, captured_at=NOW),
        dict(
            mt5_account_id=ACCOUNT, currency="USC", balance=2, captured_at=NOW + timedelta(hours=1)
        ),
    ]
    rows[-1][field] = value
    r, s = reconcile([], rows)
    assert r["trading_cash"] is None and not r["reconciled"] and len(s.calls) == 1


def test_daily_profit_excludes_capital_and_keeps_floating_separate():
    day = RunningAccountDay(
        date(2026, 11, 6),
        D("10000"),
        D("10854"),
        realised_pnl=D("44"),
        floating_pnl_change=D("10"),
        cash_movement=D("800"),
        currency="USC",
    )
    assert day.pnl == 54 and day.realised_pnl == 44 and day.floating_pnl_change == 10


def test_trade_is_not_closed_by_partial_or_missing_entry_evidence():
    service = object.__new__(Day33PerformanceLedgerService)
    row = defaultdict(
        lambda: None,
        id=uuid4(),
        user_id=USER,
        signal_id=uuid4(),
        source_id=uuid4(),
        status="open",
        symbol="XAUUSD",
        side="BUY",
        volume=D(".10"),
        source_alias="",
        original_text="",
    )

    def fill(ticket, entry, volume, profit="0", commission="0", fee="0"):
        return dict(
            cash("DEAL_TYPE_BUY", profit, commission, fee=fee),
            broker_deal_id=ticket,
            entry_type=entry,
            volume=D(volume),
            price=D("4100"),
            occurred_at=NOW,
        )

    entry = fill("1", "DEAL_ENTRY_IN", ".10", commission="-2", fee="-.50")
    partial = fill("2", "DEAL_ENTRY_OUT", ".04", "40", "-.80", "-.20")
    outcome = service._derive_outcome(None, row, [entry, partial])
    assert outcome["status"] == "open" and outcome["cash_pnl"] is None
    assert service._derive_outcome(None, row, [partial])["cash_pnl"] is None
    final = fill("3", "DEAL_ENTRY_OUT", ".06", "60", "-1.20", "-.30")
    outcome = service._derive_outcome(None, row, [entry, partial, final])
    assert outcome["cash_pnl"] == D("95.00") and outcome["status"] == "won"


def test_missing_currency_is_not_invented_as_usd():
    from app.performance_ledger_day33 import Day33LedgerError
    svc=object.__new__(Day33PerformanceLedgerService)
    with pytest.raises(Day33LedgerError,match='broker_account_currency_unavailable'):
        svc._store_snapshot(user_id=USER,mt5_account_id=ACCOUNT,payload={'balance':1,'equity':1},captured_at=NOW)


def test_account_level_commission_is_trading_cash_not_a_deposit():
    r,_=reconcile([cash('DEAL_TYPE_BALANCE','850'),cash('DEAL_TYPE_COMMISSION','-6')])
    assert r['trading_cash']==-6 and r['cash_movement']==850 and r['reconciled']
