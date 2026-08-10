"""Minimum-permission policy for the Day 19 Telegram publisher."""

from __future__ import annotations

from typing import Any

from app.telegram_publisher import TelegramPublisherManager


class Day19TelegramPublisherManager(TelegramPublisherManager):
    """Publisher that accepts only a normal group member or a post-only admin."""

    @staticmethod
    def _minimum_permissions_ok(chat_type: str, membership: dict[str, Any]) -> bool:
        status = str(membership.get("status") or "")
        if chat_type in {"group", "supergroup"} and status == "member":
            return True
        if status != "administrator":
            return False
        if chat_type == "channel" and not bool(membership.get("can_post_messages")):
            return False

        # Telegram reports can_manage_chat=True for every administrator; it is
        # informational rather than an independently granted power, so it is not
        # treated as an extra permission here. All selectable broader rights stay off.
        broad_permissions = (
            "can_change_info",
            "can_delete_messages",
            "can_invite_users",
            "can_restrict_members",
            "can_promote_members",
            "can_manage_video_chats",
            "can_manage_topics",
            "can_edit_messages",
            "can_manage_direct_messages",
            "can_post_stories",
            "can_edit_stories",
            "can_delete_stories",
        )
        return not any(bool(membership.get(name)) for name in broad_permissions)
