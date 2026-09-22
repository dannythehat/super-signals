from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from app.telegram_trade_ledger import (
    AccountLedgerSnapshot,
    TelegramTradeLedger,
    money,
    provider_badge,
)


def test_provider_badge_is_stable_and_distinguishes_sources() -> None:
    one = UUID("11111111-1111-4111-8111-111111111111")
    two = UUID("22222222-2222-4222-8222-222222222222")
    assert provider_badge(one) == provider_badge(one)
    assert provider_badge(one) != provider_badge(two)


def test_money_is_plain_signed_usd() -> None:
    assert money(Decimal("12.345")) == "+$12.35"
    assert money(Decimal("-2.5")) == "-$2.50"
    assert money(0) == "$0.00"


def test_weekday_footer_uses_vantage_value_not_synthetic_balance() -> None:
    snapshot = AccountLedgerSnapshot(
        account_value=Decimal("2050.64"),
        mt5_balance=Decimal("1436.12"),
        today_pnl=Decimal("35.20"),
        month_to_date_pnl=Decimal("142.10"),
        one_percent=Decimal("20.51"),
        local_weekday=1,
        updated_at=None,
    )
    rendered = "\n".join(
        TelegramTradeLedger.account_lines(snapshot, include_origin=True)
    )
    assert "Today: +$35.20" in rendered
    assert "Month to date: +$142.10" in rendered
    assert "Vantage account value: $2,050.64 · 1% = $20.51" in rendered
    assert "Started $1,000.00 · 6 Aug 2026" in rendered
    assert "$1,436.12" not in rendered


def test_weekend_footer_omits_daily_line() -> None:
    snapshot = AccountLedgerSnapshot(
        account_value=Decimal("2050.64"),
        mt5_balance=Decimal("1436.12"),
        today_pnl=Decimal("0"),
        month_to_date_pnl=Decimal("142.10"),
        one_percent=Decimal("20.51"),
        local_weekday=6,
        updated_at=None,
    )
    rendered = "\n".join(TelegramTradeLedger.account_lines(snapshot))
    assert "Today:" not in rendered
    assert "Month to date: +$142.10" in rendered


def test_one_percent_is_quoted_from_the_company_paper_balance() -> None:
    """The company paper balance is the Vantage demo account value, not the
    broker's closed-trade balance field.

    Sizing is always 1% of the paper balance, so the published '1% = $X' must
    track the account value shown on the same line. Quoting 1% of the broker
    balance field would understate it by the whole floating P&L.
    """
    snapshot = AccountLedgerSnapshot(
        account_value=Decimal("2050.64"),   # company paper balance
        mt5_balance=Decimal("1364.87"),     # broker closed-trade balance field
        today_pnl=Decimal("0.00"),
        month_to_date_pnl=Decimal("0.00"),
        one_percent=Decimal("20.51"),
        local_weekday=2,
        updated_at=datetime(2026, 9, 22, 12, 0, tzinfo=UTC),
        stale=False,
    )
    lines = TelegramTradeLedger.account_lines(snapshot)
    account_line = next(line for line in lines if "account value" in line)
    assert "$2,050.64" in account_line
    assert "1% = $20.51" in account_line      # 1% of the paper balance
    assert "1% = $13.64" not in account_line  # never 1% of the broker balance field


def test_stale_account_snapshot_is_never_published_as_current() -> None:
    """A stalled MetaAPI/Vantage capture must not publish a stale balance as if
    live, and must not quote a risk figure derived from it."""
    snapshot = AccountLedgerSnapshot(
        account_value=Decimal("2050.64"),
        mt5_balance=Decimal("1364.87"),
        today_pnl=Decimal("0.00"),
        month_to_date_pnl=Decimal("0.00"),
        one_percent=None,                   # suppressed by account() when stale
        local_weekday=2,
        updated_at=datetime(2026, 9, 22, 9, 30, tzinfo=UTC),
        stale=True,
    )
    lines = TelegramTradeLedger.account_lines(snapshot)
    account_line = next(line for line in lines if "account value" in line)
    assert "last updated" in account_line
    assert "1% = " not in account_line


class _StubResult:
    def __init__(self, rows: list[dict] | None = None, one: dict | None = None) -> None:
        self._rows, self._one = rows or [], one

    def mappings(self):  # noqa: ANN201
        return self

    def first(self):  # noqa: ANN201
        return self._one

    def all(self):  # noqa: ANN201
        return self._rows


class _StubSession:
    """Returns the account snapshot for the snapshot query, empty for P&L queries."""

    def __init__(self, snapshot: dict) -> None:
        self._snapshot = snapshot

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, *_exc) -> bool:  # noqa: ANN002
        return False

    def execute(self, statement, params=None):  # noqa: ANN001, ANN201, ARG002
        if "performance_account_snapshots" in str(statement):
            return _StubResult(one=self._snapshot)
        return _StubResult(rows=[])


def test_account_derives_one_percent_from_the_company_paper_balance(monkeypatch) -> None:  # noqa: ANN001
    """End-to-end derivation check, not just rendering: account() must compute the
    published 1% from the company paper balance (the Vantage demo account value),
    never from the broker's closed-trade balance field."""
    import app.telegram_trade_ledger as ledger_module

    snapshot_row = {
        "balance": Decimal("1364.87"),  # broker closed-trade balance field
        "equity": Decimal("2050.64"),   # company paper balance, floating included
        "captured_at": datetime(2026, 9, 22, 12, 0, tzinfo=UTC),
    }
    monkeypatch.setattr(
        ledger_module, "override_cash_by_day", lambda *a, **k: {}, raising=False
    )
    ledger = TelegramTradeLedger(
        lambda: _StubSession(snapshot_row),  # type: ignore[arg-type]
        UUID("00000000-0000-0000-0000-000000000001"),
    )
    snap = ledger.account(now=datetime(2026, 9, 22, 12, 1, tzinfo=UTC))

    assert snap.mt5_balance == Decimal("1364.87")
    assert snap.account_value == Decimal("2050.64")
    assert snap.one_percent == Decimal("20.51")   # 1% of the company paper balance
    assert snap.one_percent != Decimal("13.65")   # never 1% of the broker balance
    assert snap.stale is False
