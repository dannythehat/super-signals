from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from app.mt5_execution_day26 import Day26ExecutionError
from app.paper_execution_priority import PaperExecutionPriorityService


SIGNAL_ID = UUID("10000000-0000-4000-8000-000000000010")
OWNER_ID = UUID("20000000-0000-4000-8000-000000000020")
ACCOUNT_ID = UUID("30000000-0000-4000-8000-000000000030")


class _Result:
    def __init__(self, *, row=None, scalar=None):
        self._row = row
        self._scalar = scalar

    def mappings(self):
        return self

    def first(self):
        return self._row

    def scalar_one(self):
        return self._scalar


class _Session:
    def __init__(self, signal_row: dict):
        self.signal_row = signal_row

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, statement, params=None):
        sql = str(statement)
        if "FROM signals" in sql and "SELECT EXISTS" not in sql:
            return _Result(row=self.signal_row)
        if "SELECT COUNT(*)" in sql and "FROM positions" in sql:
            return _Result(scalar=0)
        if "SELECT EXISTS" in sql and "signal_lifecycle_events" in sql:
            return _Result(scalar=False)
        if "FROM mt5_accounts" in sql:
            return _Result(
                row={
                    "id": ACCOUNT_ID,
                    "metaapi_account_id": "demo-account",
                    "metaapi_token_ciphertext": b"encrypted-token",
                    "account_environment": "demo",
                    "status": "connected",
                }
            )
        raise AssertionError(f"unexpected SQL: {sql}")


class _Factory:
    def __init__(self, signal_row: dict):
        self.signal_row = signal_row

    def __call__(self):
        return _Session(self.signal_row)


def _row(raw_text: str = "Gold sell now") -> dict:
    return {
        "id": SIGNAL_ID,
        "symbol": "XAUUSD",
        "side": "SELL",
        "order_type": "market",
        "entry_low": None,
        "entry_high": None,
        "stop_loss": None,
        "take_profits": [],
        "has_open_runner": False,
        "parser_status": "accepted",
        "risk_multiplier": Decimal("1"),
        "source_revision_index": 0,
        "source_posted_at": datetime.now(UTC),
        "original_text": raw_text,
    }


def _service(raw_text: str = "Gold sell now") -> PaperExecutionPriorityService:
    service = object.__new__(PaperExecutionPriorityService)
    service._session_factory = _Factory(_row(raw_text))
    return service


def test_exact_live_gold_sell_now_survives_critical_loader_without_provider_tp() -> None:
    critical = _service()._load_critical_signal(SIGNAL_ID)

    assert critical.original_text == "Gold sell now"
    assert critical.broad_order_type == "market"
    assert critical.base.side == "SELL"
    assert critical.base.entry_low == Decimal("0")
    assert critical.base.entry_high == Decimal("0")
    assert critical.base.stop_loss == Decimal("0")
    assert critical.base.take_profits == ()


def test_exact_live_gold_sell_now_survives_day28_loader_without_provider_tp() -> None:
    signal, account = _service()._load_inputs(OWNER_ID, SIGNAL_ID)

    assert signal.side == "SELL"
    assert signal.take_profits == ()
    assert signal.stop_loss == Decimal("0")
    assert account.local_account_id == ACCOUNT_ID
    assert account.metaapi_account_id == "demo-account"


def test_other_incomplete_signal_still_fails_closed() -> None:
    with pytest.raises(Day26ExecutionError) as exc:
        _service("SELL GOLD")._load_critical_signal(SIGNAL_ID)

    assert exc.value.code == "signal_take_profits_invalid"
