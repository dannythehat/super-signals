from uuid import UUID

from app.telegram_publisher_day34_cutover import Day34CutoverTelegramPublisherManager
from app.trade_identity import prefix_public_trade_identity, public_trade_identity


def test_public_trade_identity_is_stable_and_provider_hidden() -> None:
    signal_id = UUID("cbc7baf8-8819-4ece-978d-91dbd9f8a9cd")

    first = public_trade_identity(signal_id)
    second = public_trade_identity(str(signal_id))

    assert first == second
    assert first.reference == "SS-CBC7BAF888"
    assert first.marker in {"🟣", "🟪", "🔷", "🟧", "🔶", "🔹", "🔸", "💠"}
    assert "provider" not in first.label.lower()


def test_lifecycle_wording_keeps_the_same_trade_reference() -> None:
    signal_id = UUID("cbc7baf8-8819-4ece-978d-91dbd9f8a9cd")
    identity = public_trade_identity(signal_id)

    rendered = prefix_public_trade_identity(
        signal_id,
        "❌ XAUUSD · TP1 position closed\n-1 pips",
    )

    assert rendered.startswith(f"{identity.label} · ❌ XAUUSD")
    assert "TP1 position closed" in rendered
    assert "-1 pips" in rendered


def test_live_board_distinguishes_three_overlapping_same_symbol_trades() -> None:
    signal_ids = [
        UUID("11111111-1111-4111-8111-111111111111"),
        UUID("22222222-2222-4222-8222-222222222222"),
        UUID("33333333-3333-4333-8333-333333333333"),
    ]
    rows = [
        {
            "signal_id": signal_ids[0],
            "symbol": "XAUUSD",
            "side": "BUY",
            "open_tp_indices": [2, 3],
            "pending_tp_indices": [],
        },
        {
            "signal_id": signal_ids[1],
            "symbol": "XAUUSD",
            "side": "SELL",
            "open_tp_indices": [1],
            "pending_tp_indices": [],
        },
        {
            "signal_id": signal_ids[2],
            "symbol": "XAUUSD",
            "side": "BUY",
            "open_tp_indices": [],
            "pending_tp_indices": [1, 2],
        },
    ]

    rendered = Day34CutoverTelegramPublisherManager._render_live_board(rows)
    references = [public_trade_identity(signal_id).reference for signal_id in signal_ids]

    assert "OPEN 2 · PENDING 1" in rendered
    assert len(set(references)) == 3
    for reference in references:
        assert rendered.count(reference) == 1
    assert "XAUUSD BUY · TP2/TP3 open" in rendered
    assert "XAUUSD SELL · TP1 open" in rendered
    assert "XAUUSD BUY · TP1/TP2 pending" in rendered
    assert "provider" not in rendered.lower()
    assert "account" not in rendered.lower()


def test_closed_trade_can_disappear_without_changing_other_trade_identities() -> None:
    closed_id = UUID("11111111-1111-4111-8111-111111111111")
    remaining_ids = [
        UUID("22222222-2222-4222-8222-222222222222"),
        UUID("33333333-3333-4333-8333-333333333333"),
    ]
    remaining_rows = [
        {
            "signal_id": remaining_ids[0],
            "symbol": "XAUUSD",
            "side": "SELL",
            "open_tp_indices": [1],
            "pending_tp_indices": [],
        },
        {
            "signal_id": remaining_ids[1],
            "symbol": "XAUUSD",
            "side": "BUY",
            "open_tp_indices": [3],
            "pending_tp_indices": [],
        },
    ]

    rendered = Day34CutoverTelegramPublisherManager._render_live_board(remaining_rows)

    assert public_trade_identity(closed_id).reference not in rendered
    for signal_id in remaining_ids:
        assert public_trade_identity(signal_id).reference in rendered
    assert "OPEN 2 · PENDING 0" in rendered
