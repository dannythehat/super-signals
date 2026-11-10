"""Gold master reset account messages cannot borrow Vantage account performance."""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.owner_demo_account_reporting import (
    OwnerDemoAccountReport, _body, _validate, reporting_day_start,
)


def sample(captured: datetime | None = None) -> OwnerDemoAccountReport:
    captured = captured or datetime(2026, 11, 9, 16, 56, 5, tzinfo=timezone.utc)
    return OwnerDemoAccountReport(
        master_login=700207861, server="PUPrime-Demo", trade_mode="demo",
        currency="USD", connected=True, balance=Decimal("10090.26"),
        equity=Decimal("10090.26"), floating_pnl=Decimal("0"),
        credit=Decimal("0"), realised_net=Decimal("90.26"),
        cash_movements=Decimal("0"), closed_deal_fills=5,
        open_positions=0, reporting_start_utc=reporting_day_start(captured),
        captured_at=captured,
    )


def test_new_demo_balance_and_daily_net_do_not_use_legacy_vantage():
    report = sample()
    start = _validate(
        report, now=report.captured_at + timedelta(seconds=5)
    )
    text = _body(report, start)
    assert "PU Prime DEMO | Master 700207861" in text
    assert "Balance: $10,090.26" in text
    assert "Today's realised P&L: +$90.26" in text
    assert "Equity: $10,090.26" in text
    assert "08 Nov 21:00 to 09 Nov 21:00" in text
    assert "Vantage" not in text


def test_21h_sofia_day_boundary_across_dst():
    # Winter cutoff 19:00 UTC, summer cutoff 18:00 UTC.
    assert reporting_day_start(datetime(2026,11,9,16,56,tzinfo=timezone.utc)) == datetime(2026,11,8,19,tzinfo=timezone.utc)
    assert reporting_day_start(datetime(2026,7,9,17,56,tzinfo=timezone.utc)) == datetime(2026,7,8,18,tzinfo=timezone.utc)


def test_stale_or_incorrect_account_read_is_rejected():
    report = sample()
    with pytest.raises(HTTPException) as exc:
        _validate(report, now=report.captured_at + timedelta(minutes=5))
    assert exc.value.status_code == 409
    report = report.model_copy(update={"equity": Decimal("9800")})
    with pytest.raises(HTTPException) as exc:
        _validate(report, now=report.captured_at + timedelta(seconds=5))
    assert exc.value.detail == "broker_equity_mismatch"


@pytest.mark.parametrize("relay_route", ("confirmed-execution", "broker-publication"))
def test_broker_report_accepts_both_existing_relay_routes(monkeypatch, relay_route):
    account = SimpleNamespace(
        login=700207861, server="PUPrime-Demo", trade_mode=0, currency="USD",
        balance=10090.26, equity=10090.26, profit=0, credit=0,
    )
    mt5 = SimpleNamespace(
        ACCOUNT_TRADE_MODE_DEMO=0, initialize=lambda **_: True,
        account_info=lambda: account, terminal_info=lambda: SimpleNamespace(connected=True),
        history_deals_get=lambda *_: [], positions_get=lambda: [], shutdown=lambda: None,
    )
    base = "https://super-signals.example/internal/local-bridge/v1/owner-demo-700207861-"
    relay = SimpleNamespace(credentials=lambda: (base + relay_route, "test-credential"))
    monkeypatch.setitem(sys.modules, "MetaTrader5", mt5)
    monkeypatch.setitem(sys.modules, "owner_master_telegram_relay", relay)
    script = Path(__file__).resolve().parents[3] / "scripts" / "owner_demo_account_reporter.py"
    verified_snapshot = runpy.run_path(str(script))["verified_snapshot"]
    monkeypatch.setitem(verified_snapshot.__globals__, "offset_from_execution_journal", lambda *_, **__: 2)

    url, _, payload, _ = verified_snapshot()
    assert url == base + "account-report"
    assert payload["master_login"] == 700207861
    assert payload["balance"] == "10090.26"
