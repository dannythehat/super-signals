"""Minimum-permission policy and live connection test for the Day 19 publisher."""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import text

from app.models import AuditEvent
from app.telegram_publisher import (
    PUBLISHER_VERSION,
    PublisherConnectionStatus,
    TelegramPublishError,
    TelegramPublisherManager,
    _bot_api_call,
)


class Day19TelegramPublisherManager(TelegramPublisherManager):
    """Publisher that accepts only a normal group member or a post-only admin."""

    def __init__(self, *, reader_exclusion_active: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._reader_exclusion_active = reader_exclusion_active

    async def start(self) -> None:
        await super().start()
        status = await asyncio.to_thread(self.check_connection)
        with self._session_factory() as session:
            session.add(
                AuditEvent(
                    actor_user_id=None,
                    event_type="telegram.publisher_startup_status",
                    entity_type="telegram_publisher",
                    entity_id=None,
                    payload={
                        "publisher_version": PUBLISHER_VERSION,
                        "configured": status.configured,
                        "enabled": status.enabled,
                        "destination_chat_type": status.destination_chat_type,
                        "bot_membership_status": status.bot_membership_status,
                        "minimum_permissions_ok": status.minimum_permissions_ok,
                        "source_collision": status.source_collision,
                        "reason": status.reason,
                        "reader_exclusion_active": self._reader_exclusion_active,
                    },
                )
            )
            session.commit()

    @staticmethod
    def _minimum_permissions_ok(chat_type: str, membership: dict[str, Any]) -> bool:
        status = str(membership.get("status") or "")
        if chat_type in {"group", "supergroup"} and status == "member":
            return True
        if status != "administrator":
            return False
        if chat_type == "channel" and not bool(membership.get("can_post_messages")):
            return False

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

    def _active_reader_source_collision(self) -> bool:
        if self._reader_exclusion_active:
            return False

        assert self._destination_chat_id is not None
        with self._session_factory() as session:
            return bool(
                session.execute(
                    text(
                        """
                        SELECT 1 FROM sources
                        WHERE chat_id = :chat_id
                          AND status IN ('testing', 'live')
                        LIMIT 1
                        """
                    ),
                    {"chat_id": self._destination_chat_id},
                ).scalar_one_or_none()
            )

    def check_connection(self) -> PublisherConnectionStatus:
        if not self._enabled:
            return PublisherConnectionStatus(
                configured=self.configured,
                enabled=False,
                destination_chat_type=None,
                bot_membership_status=None,
                minimum_permissions_ok=False,
                source_collision=False,
                reason="Publisher is disabled.",
            )
        if not self.configured:
            return PublisherConnectionStatus(
                configured=False,
                enabled=True,
                destination_chat_type=None,
                bot_membership_status=None,
                minimum_permissions_ok=False,
                source_collision=False,
                reason="Publisher bot token and destination chat ID must both be configured.",
            )
        assert self._bot_token is not None
        assert self._destination_chat_id is not None

        if self._active_reader_source_collision():
            return PublisherConnectionStatus(
                configured=True,
                enabled=True,
                destination_chat_type=None,
                bot_membership_status=None,
                minimum_permissions_ok=False,
                source_collision=True,
                reason="Publishing destination is still active in the private-reader plan.",
            )

        try:
            me = _bot_api_call(self._bot_token, "getMe", {})
            chat = _bot_api_call(
                self._bot_token,
                "getChat",
                {"chat_id": self._destination_chat_id},
            )
            membership = _bot_api_call(
                self._bot_token,
                "getChatMember",
                {"chat_id": self._destination_chat_id, "user_id": int(me["id"])},
            )
        except (TelegramPublishError, KeyError, TypeError, ValueError) as exc:
            reason = (
                exc.reason
                if isinstance(exc, TelegramPublishError)
                else "Telegram bot identity could not be verified."
            )
            return PublisherConnectionStatus(
                configured=True,
                enabled=True,
                destination_chat_type=None,
                bot_membership_status=None,
                minimum_permissions_ok=False,
                source_collision=False,
                reason=reason,
            )

        chat_type = str(chat.get("type") or "")
        membership_status = str(membership.get("status") or "")
        minimum_ok = self._minimum_permissions_ok(chat_type, membership)
        return PublisherConnectionStatus(
            configured=True,
            enabled=True,
            destination_chat_type=chat_type or None,
            bot_membership_status=membership_status or None,
            minimum_permissions_ok=minimum_ok,
            source_collision=False,
            reason=(
                "Publish-only Telegram connection verified."
                if minimum_ok
                else "Bot permissions are broader than required or do not allow posting."
            ),
        )

    def send_connection_test(self) -> dict[str, Any]:
        if not self._enabled:
            return {
                "status": "blocked",
                "reason": "Publisher is disabled.",
                "telegram_message_id": None,
            }
        if not self.configured:
            return {
                "status": "blocked",
                "reason": "Publisher bot token and destination chat ID must both be configured.",
                "telegram_message_id": None,
            }
        assert self._bot_token is not None
        assert self._destination_chat_id is not None

        if self._active_reader_source_collision():
            return {
                "status": "blocked",
                "reason": "Publishing destination is still active in the private-reader plan.",
                "telegram_message_id": None,
            }

        status = "sent"
        failure_code: str | None = None
        reason = "Connection test was posted to the configured Super Signals destination."
        telegram_message_id: int | None = None
        try:
            result = _bot_api_call(
                self._bot_token,
                "sendMessage",
                {
                    "chat_id": self._destination_chat_id,
                    "text": "Super Signals publisher connection test\nNo trade was created.",
                    "disable_web_page_preview": "true",
                },
            )
            telegram_message_id = int(result["message_id"])
        except TelegramPublishError as exc:
            status = "failed"
            failure_code = exc.code
            reason = exc.reason
        except (KeyError, TypeError, ValueError):
            status = "failed"
            failure_code = "telegram_invalid_success_response"
            reason = "Telegram did not return a usable destination message ID."

        with self._session_factory() as session:
            session.add(
                AuditEvent(
                    actor_user_id=None,
                    event_type=(
                        "telegram.publisher_test_sent"
                        if status == "sent"
                        else "telegram.publisher_test_failed"
                    ),
                    entity_type="telegram_publisher",
                    entity_id=None,
                    payload={
                        "publisher_version": PUBLISHER_VERSION,
                        "status": status,
                        "failure_code": failure_code,
                        "telegram_message_id": telegram_message_id,
                        "configured_destination_only": True,
                        "provider_identity_exposed": False,
                        "reader_session_used": False,
                        "canonical_signal_unchanged": True,
                        "position_created": False,
                        "trade_action_created": False,
                    },
                )
            )
            session.commit()

        return {
            "status": status,
            "reason": reason[:300],
            "telegram_message_id": telegram_message_id,
        }
