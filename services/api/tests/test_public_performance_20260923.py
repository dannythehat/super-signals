"""Public website figures for 23 Sep 2026.

Two problems on the public performance page that day:

1. The daily figure switched from Vantage equity to the closed MT5 balance at 13:02 UTC,
   but 23 Sep kept its fixed opening of 2075.81 - the 22 Sep *equity*. The day was
   published as closed balance minus yesterday's equity, a loss of about $737 that
   never happened. Opening and closing must be the same field.
2. The owner reversed TRADE GLOBAL's and Scalping 📈's cash for the day on the paper
   account, so their trades must not appear in the public trade log either.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

from app.routes import gold_quote
from app.running_daily_balance import opening_account_value

TRADE_GLOBAL = -1003925988158
SCALPING = -1004469449988


class _NoQuerySession:
    def execute(self, *_args, **_kwargs):  # pragma: no cover - failing is the assertion
        raise AssertionError("23 Sep has a fixed opening and must not read snapshots")


def test_23_sep_opens_at_the_22_sep_closing_balance_not_equity() -> None:
    opening = opening_account_value(_NoQuerySession(), uuid4(), date(2026, 9, 23))
    assert opening == Decimal("1396.87")


def test_trade_global_and_scalping_are_matched_by_chat_id() -> None:
    assert set(gold_quote._PUBLIC_TRADE_LOG_EXCLUDED_CHAT_IDS) == {TRADE_GLOBAL, SCALPING}
    assert gold_quote._PUBLIC_TRADE_LOG_EXCLUDED_FROM == date(2026, 9, 23)


@dataclass
class _Trade:
    signal_id: UUID
    opened_at: datetime
    position_count: int = 2
    symbol: str = "XAUUSD"
    side: str = "BUY"
    status: str = "won"
    status_label: str = "Won"
    closed_at: datetime | None = None
    open_positions: int = 0
    pending_positions: int = 0
    closed_positions: int = 2
    cash_pnl: Decimal | None = Decimal("10")
    net_pips: Decimal | None = Decimal("5")
    close_reason: str | None = None


class _Service:
    def __init__(self, trades: list[_Trade], excluded: set[UUID]) -> None:
        self._trades = trades
        self._excluded = excluded
        self.sql: list[str] = []

    @contextmanager
    def _session_factory(self):
        service = self

        class _Session:
            def execute(self, statement, _params):
                service.sql.append(str(statement))
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(service._excluded)))

        yield _Session()

    def read_timeline(self, _user_id, *, viewer_role, limit):
        assert viewer_role == "user"
        return SimpleNamespace(trades=tuple(self._trades))


def test_excluded_providers_leave_the_trade_log_from_23_sep_only() -> None:
    kept_today = _Trade(uuid4(), datetime(2026, 9, 23, 9, 0, tzinfo=UTC))
    excluded_today = _Trade(uuid4(), datetime(2026, 9, 23, 12, 0, tzinfo=UTC))
    excluded_earlier = _Trade(uuid4(), datetime(2026, 9, 22, 9, 0, tzinfo=UTC))
    service = _Service(
        [kept_today, excluded_today, excluded_earlier],
        {excluded_today.signal_id, excluded_earlier.signal_id},
    )

    trades = gold_quote._public_trades(service, uuid4())  # type: ignore[arg-type]

    ids = {trade.signal_id for trade in trades}
    assert kept_today.signal_id in ids
    assert excluded_today.signal_id not in ids
    # Earlier days keep their trades: their published P/L still includes them.
    assert excluded_earlier.signal_id in ids
    assert str(TRADE_GLOBAL) in service.sql[0] and str(SCALPING) in service.sql[0]
