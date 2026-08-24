from datetime import UTC, datetime, timedelta

from decimal import Decimal
from types import SimpleNamespace

from app.canonical_signal_ledger import (
    _duplicate_window,
    _explicit_additional_trade,
    _semantic_duplicate,
)


def test_duplicate_window_covers_repost_after_earlier_message() -> None:
    posted_at = datetime(2026, 8, 21, 11, 1, 45, tzinfo=UTC)
    start, end = _duplicate_window(posted_at)

    assert start == posted_at - timedelta(seconds=60)
    assert end == posted_at + timedelta(seconds=60)
    # Production incident: corrected repost 61509 arrived 10 seconds after 61508.
    assert start <= posted_at + timedelta(seconds=10) <= end


def test_duplicate_window_excludes_genuinely_later_identical_setup() -> None:
    posted_at = datetime(2026, 8, 21, 11, 1, 45, tzinfo=UTC)
    start, end = _duplicate_window(posted_at)

    assert not (start <= posted_at + timedelta(seconds=61) <= end)



def test_fxtradingvision_shifted_repost_is_one_logical_trade() -> None:
    trade = SimpleNamespace(
        entry_low=Decimal("4673"), entry_high=Decimal("4673"),
        stop_loss=Decimal("4645"),
        take_profits=(Decimal("4677"), Decimal("4678"), Decimal("4700")),
    )
    earlier = {
        "entry_low": Decimal("4674"), "entry_high": Decimal("4674"),
        "stop_loss": Decimal("4650"), "take_profits": ["4678", "4679", "4700"],
    }
    assert _semantic_duplicate(trade, earlier)


def test_explicit_additional_trade_bypasses_semantic_duplicate_guard() -> None:
    assert _explicit_additional_trade("SECOND TRADE: XAUUSD BUY 4673")
    assert _explicit_additional_trade("Gold re-entry BUY 4673")
    assert not _explicit_additional_trade("NEW TRADE IDEA XAUUSD BUY 4673")
