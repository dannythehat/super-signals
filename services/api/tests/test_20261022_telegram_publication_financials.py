"""Regression tests: every genuine member settlement shows proven account values.

The event leg cash still comes from the broker. A separate publication-time
broker/website snapshot is clearly labelled rather than pretending an unproved
cash-step was the balance immediately after one TP.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from app.telegram_publisher_canonical import CanonicalTelegramPublisherManager


class _NoBrokerDeal:
    """Broker deal lookup returns empty, so historical per-TP proof is missing."""

    class _Mapping:
        def first(self):
            return None

    def execute(self, statement, params=None):
        assert "FROM broker_deals" in str(statement)
        return self

    def mappings(self):
        return self._Mapping()


def test_settlement_missing_cash_step_displays_publication_account_numbers() -> None:
    manager = object.__new__(CanonicalTelegramPublisherManager)
    manager._reference_user_id = uuid4()

    lines = manager._settlement_financial_lines(
        _NoBrokerDeal(),
        event_at=datetime(2026, 10, 22, 9, 3, tzinfo=UTC),
        position_id=uuid4(),
        publication_balance=Decimal("3181.92"),
        publication_daily_pnl=Decimal("53.98"),
    )
    output = "\n".join(lines)
    assert "Balance (at time of update)" in output
    assert "$3,181.92" in output
    assert "Today's P&L (at time of update)" in output
    assert "+$53.98" in output
    assert "confirming" not in output.lower()
    assert "Balance after this TP" not in output


def test_sent_placeholder_repair_uses_that_messages_own_reserved_numbers() -> None:
    original = (
        "<b>TRADE LOSS · -$28.77</b>\n"
        "<b>Balance after this TP</b>\n"
        "<b>Broker snapshot confirming…</b>\n"
        "<b>Today's P&L after this TP</b>\n"
        "<b>Broker snapshot confirming…</b>"
    )
    fixed = CanonicalTelegramPublisherManager._repair_publication_time_financial_lines(
        original, Decimal("3168.98"), Decimal("41.04")
    )
    assert "<b>TRADE LOSS · -$28.77</b>" in fixed
    assert "<b>Balance (at time of update)</b>\n<b>$3,168.98</b>" in fixed
    assert "<b>Today's P&L (at time of update)</b>\n<b>+$41.04</b>" in fixed
    assert "confirming" not in fixed
    assert (
        CanonicalTelegramPublisherManager._repair_publication_time_financial_lines(
            fixed, Decimal("9000"), Decimal("-100")
        ) == fixed
    )


def test_repair_never_replaces_broker_proven_historical_balance() -> None:
    proven = (
        "<b>Balance after this TP</b>\n<b>$3,090.23</b>\n"
        "<b>Today's P&L after this TP</b>\n<b>-$37.71</b>"
    )
    fixed = CanonicalTelegramPublisherManager._repair_publication_time_financial_lines(
        proven, Decimal("3181.92"), Decimal("53.98")
    )
    assert fixed == proven
