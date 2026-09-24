"""TRADE GLOBAL and Scalping 📈 must not appear in the public trade log from 23 Sep 2026.

Owner instruction, after the TRADE GLOBAL incident (migration 0116) and the Scalping
shadow fix (migration 0118): the owner reversed their cash impact for the day on the
paper account, so the 21:00-equity daily P/L already reflects that (see
`running_daily_balance.py`). This only keeps their individual rows out of the public
trade log. Earlier days keep their trades, since those days' published P/L still
includes them.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

from app.routes import gold_quote

TRADE_GLOBAL = -1003925988158
SCALPING = -1004469449988


def test_excluded_chat_ids_and_cutover_date() -> None:
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


def test_late_evening_trade_uses_the_21_00_sofia_day_boundary() -> None:
    """A 22:00 Sofia trade on 22 Sep reports as 23 Sep, so it must be checked too."""
    late_trade = _Trade(uuid4(), datetime(2026, 9, 22, 19, 5, tzinfo=UTC))  # 22:05 Sofia
    service = _Service([late_trade], {late_trade.signal_id})

    trades = gold_quote._public_trades(service, uuid4())  # type: ignore[arg-type]

    assert late_trade.signal_id not in {trade.signal_id for trade in trades}
