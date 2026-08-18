from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import app.performance_account_truth_override as account_truth
import app.telegram_publisher as publisher


def test_sparse_signal_publication_never_crashes_decimal_rendering() -> None:
    assert publisher._decimal_text(None) == "N/A"
    row = {
        "symbol": "XAUUSD",
        "side": "BUY",
        "entry_price": None,
        "stop_loss": None,
        "take_profits": [],
        "has_open_runner": False,
        "risk_multiplier": 1,
    }
    rendered = publisher.render_signal_post(row)
    assert "Entry: N/A" in rendered
    assert "Stop Loss: N/A" in rendered


class _Result:
    @staticmethod
    def first():
        return (1,)


class _Session:
    def __init__(self, statements: list[str]) -> None:
        self.statements = statements
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, statement, params=None):
        self.statements.append(str(statement))
        return _Result()

    def commit(self):
        self.committed = True


class _LedgerHarness:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self._session_factory = lambda: _Session(self.statements)

    @staticmethod
    def _mapped_positions(user_id):
        return []


def test_account_truth_sync_obeys_append_only_broker_deal_ledger() -> None:
    harness = _LedgerHarness()
    added = account_truth._store_account_deals(
        harness,
        user_id=uuid4(),
        mt5_account_id=uuid4(),
        payloads=[
            {
                "id": "deal-1",
                "type": "DEAL_TYPE_BUY",
                "time": datetime.now(UTC).isoformat(),
                "symbol": "XAUUSD",
                "volume": 0.01,
                "price": 4400.0,
                "profit": 0,
                "commission": 0,
                "swap": 0,
            }
        ],
    )
    assert added == 1
    sql = "\n".join(harness.statements).upper()
    assert "ON CONFLICT" in sql
    assert "DO NOTHING" in sql
    assert "DO UPDATE" not in sql
