from uuid import UUID

from app.telegram_publisher_day34_cutover import Day34CutoverTelegramPublisherManager
from app.trade_identity import prefix_public_trade_identity, public_trade_identity


def test_numbered_trade_identity_is_plain_stable_and_coloured() -> None:
    signal_id = UUID("cbc7baf8-8819-4ece-978d-91dbd9f8a9cd")

    first = public_trade_identity(signal_id, 1)
    second = public_trade_identity(str(signal_id), 1)

    assert first == second
    assert first.reference == "TRADE 1"
    assert first.marker == "🔵"
    assert public_trade_identity(signal_id, 2).marker == "🟢"
    assert "provider" not in first.label.lower()
    assert "SS-" not in first.label


def test_lifecycle_update_reads_as_trade_number_update() -> None:
    signal_id = UUID("cbc7baf8-8819-4ece-978d-91dbd9f8a9cd")

    rendered = prefix_public_trade_identity(
        signal_id,
        "TRADE UPDATE\nTP1 reached.",
        1,
    )

    assert rendered == "🔵 TRADE 1 UPDATE — TP1 reached."


def test_broker_final_result_reads_as_numbered_trade_closed() -> None:
    signal_id = UUID("cbc7baf8-8819-4ece-978d-91dbd9f8a9cd")

    rendered = prefix_public_trade_identity(
        signal_id,
        "🎉🎉 TRADE CLOSED — WIN 🎉🎉\nXAUUSD BUY\nPROFIT: +50 pips",
        2,
    )

    assert rendered.startswith("🎉🎉 🟢 TRADE 2 CLOSED — WIN 🎉🎉")
    assert "PROFIT: +50 pips" in rendered


def test_live_board_uses_simple_numbered_coloured_trades() -> None:
    rows = [
        {
            "signal_id": UUID("11111111-1111-4111-8111-111111111111"),
            "member_trade_number": 1,
            "symbol": "XAUUSD",
            "side": "BUY",
            "open_tp_indices": [2, 3],
            "pending_tp_indices": [],
        },
        {
            "signal_id": UUID("22222222-2222-4222-8222-222222222222"),
            "member_trade_number": 2,
            "symbol": "XAUUSD",
            "side": "SELL",
            "open_tp_indices": [1],
            "pending_tp_indices": [],
        },
        {
            "signal_id": UUID("33333333-3333-4333-8333-333333333333"),
            "member_trade_number": 3,
            "symbol": "XAUUSD",
            "side": "BUY",
            "open_tp_indices": [],
            "pending_tp_indices": [1, 2],
        },
    ]

    rendered = Day34CutoverTelegramPublisherManager._render_live_board(rows)

    assert "OPEN 2 · PENDING 1" in rendered
    assert "🔵 TRADE 1 · XAUUSD BUY · TP2/TP3 open" in rendered
    assert "🟢 TRADE 2 · XAUUSD SELL · TP1 open" in rendered
    assert "🟣 TRADE 3 · XAUUSD BUY · TP1/TP2 pending" in rendered
    assert "SS-" not in rendered


def test_closed_slot_can_be_reused_without_renumbering_an_open_trade() -> None:
    new_trade = public_trade_identity(
        UUID("44444444-4444-4444-8444-444444444444"),
        1,
    )
    still_open = public_trade_identity(
        UUID("22222222-2222-4222-8222-222222222222"),
        2,
    )

    assert new_trade.label == "🔵 TRADE 1"
    assert still_open.label == "🟢 TRADE 2"
