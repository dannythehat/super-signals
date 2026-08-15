from decimal import Decimal

from app.telegram_publisher import render_signal_post
from app.telegram_publisher_policy import Day19TelegramPublisherManager


def test_render_signal_post_uses_only_canonical_fields() -> None:
    row = {
        "symbol": "XAUUSD",
        "side": "SELL",
        "entry_price": Decimal("4130"),
        "stop_loss": Decimal("4145"),
        "take_profits": ["4120", "4110"],
        "risk_multiplier": Decimal("1"),
        "source_title": "DO NOT LEAK PROVIDER",
        "provider_message_id": 999,
        "original_text": "provider private wording",
    }
    text = render_signal_post(row)
    assert text == (
        "SUPER SIGNALS\n\n"
        "XAUUSD SELL\n"
        "Entry: 4130\n"
        "Stop Loss: 4145\n"
        "TP1: 4120\n"
        "TP2: 4110\n"
        "Size: Standard"
    )
    assert "DO NOT LEAK PROVIDER" not in text
    assert "999" not in text
    assert "provider private wording" not in text


def test_render_double_size_instruction() -> None:
    text = render_signal_post(
        {
            "symbol": "XAUUSD",
            "side": "BUY",
            "entry_price": "4000",
            "stop_loss": "3990",
            "take_profits": ["4010"],
            "risk_multiplier": "2",
        }
    )
    assert text.endswith("Size: Double")


def test_render_open_runner_as_the_next_tp() -> None:
    """Regression for GTMO 60845: four numeric TPs plus TP open must never look
    fully finished in the member root post while the fifth broker leg is running."""
    text = render_signal_post(
        {
            "symbol": "XAUUSD",
            "side": "BUY",
            "entry_price": "4347",
            "stop_loss": "4342",
            "take_profits": ["4353", "4355", "4357", "4359"],
            "has_open_runner": True,
            "risk_multiplier": "1",
        }
    )
    assert text == (
        "SUPER SIGNALS\n\n"
        "XAUUSD BUY\n"
        "Entry: 4347\n"
        "Stop Loss: 4342\n"
        "TP1: 4353\n"
        "TP2: 4355\n"
        "TP3: 4357\n"
        "TP4: 4359\n"
        "TP5: OPEN\n"
        "Size: Standard"
    )


def test_minimum_permissions_accepts_plain_group_member() -> None:
    assert Day19TelegramPublisherManager._minimum_permissions_ok(
        "supergroup", {"status": "member"}
    )


def test_minimum_permissions_accepts_post_only_channel_admin() -> None:
    assert Day19TelegramPublisherManager._minimum_permissions_ok(
        "channel",
        {
            "status": "administrator",
            "can_manage_chat": True,
            "can_post_messages": True,
            "can_delete_messages": False,
            "can_change_info": False,
            "can_invite_users": False,
            "can_promote_members": False,
            "can_edit_messages": False,
            "can_post_stories": False,
        },
    )


def test_minimum_permissions_rejects_broad_admin_rights() -> None:
    assert not Day19TelegramPublisherManager._minimum_permissions_ok(
        "channel",
        {
            "status": "administrator",
            "can_manage_chat": True,
            "can_post_messages": True,
            "can_delete_messages": True,
        },
    )
