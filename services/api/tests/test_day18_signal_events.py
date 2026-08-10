from datetime import datetime, timezone
from decimal import Decimal

from app.signal_events import build_signal_fingerprint


def _fingerprint(**overrides: object) -> str:
    values: dict[str, object] = {
        "provider_chat_id": -1001234567890,
        "provider_message_id": 4047,
        "source_posted_at": datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc),
        "symbol": "XAUUSD",
        "side": "SELL",
        "entry_price": Decimal("4047.0000000000"),
        "stop_loss": Decimal("4070.0000000000"),
        "take_profits": [Decimal("4043"), Decimal("4042.0"), Decimal("4000.000")],
        "size_multiplier": Decimal("2.0000"),
        "order_type": "market",
    }
    values.update(overrides)
    return build_signal_fingerprint(**values)  # type: ignore[arg-type]


def test_fingerprint_is_stable_across_decimal_representation() -> None:
    first = _fingerprint()
    second = _fingerprint(
        entry_price="4047",
        stop_loss="4070.0",
        take_profits=["4043.000", "4042", "4000"],
        size_multiplier="2",
    )
    assert first == second
    assert len(first) == 64


def test_same_provider_message_identity_is_part_of_fingerprint() -> None:
    assert _fingerprint(provider_message_id=4047) != _fingerprint(provider_message_id=4048)


def test_source_timestamp_is_part_of_fingerprint() -> None:
    assert _fingerprint() != _fingerprint(
        source_posted_at=datetime(2026, 8, 10, 10, 0, 1, tzinfo=timezone.utc)
    )


def test_trade_details_are_part_of_fingerprint() -> None:
    assert _fingerprint() != _fingerprint(stop_loss="4071")
    assert _fingerprint() != _fingerprint(take_profits=["4043", "4041", "4000"])
