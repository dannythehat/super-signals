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
