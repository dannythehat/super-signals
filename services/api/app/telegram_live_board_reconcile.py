"""Best-effort startup reconcile for the pinned Super Signals live-trades board.

The normal Day 34 publisher updates one bot-authored board in place whenever the
broker-backed trade state changes. Telegram group migrations or an older pinned
selection can still leave a client showing a stale pin even when PostgreSQL already
contains the correct board text. This startup repair forces the stored board message
to the freshly rendered state and re-pins that exact message so the group opens on the
current live board.

This module is notification-only. It never calls MT5 execution or management code and
must never block application startup if Telegram is unavailable.
"""

from __future__ import annotations

import hashlib
import logging

from sqlalchemy import text

from app.db import get_session_factory
from app.publisher_config import get_publisher_settings
from app.telegram_publisher import TelegramPublishError, _bot_api_call
from app.telegram_publisher_day34_cutover import Day34CutoverTelegramPublisherManager

logger = logging.getLogger(__name__)


def _current_live_board_message_id(
    manager: Day34CutoverTelegramPublisherManager,
) -> int | None:
    with manager._session_factory() as session:
        value = session.execute(
            text(
                """
                SELECT telegram_message_id
                FROM telegram_live_board_state
                WHERE id = 1
                """
            )
        ).scalar_one()
    return int(value) if value is not None else None


def _is_message_not_modified(exc: TelegramPublishError) -> bool:
    return (
        exc.code == "telegram_http_400"
        and "message is not modified" in exc.reason.lower()
    )


def _is_benign_unpin(exc: TelegramPublishError) -> bool:
    if exc.code != "telegram_http_400":
        return False
    reason = exc.reason.lower()
    return "message is not pinned" in reason or "message to unpin not found" in reason


def force_reconcile_live_board(manager: Day34CutoverTelegramPublisherManager) -> bool:
    """Force Telegram's board to current DB truth and make it the active pin."""

    # First run the normal state-change logic. This creates/replaces the board if the
    # stored Telegram message genuinely no longer exists.
    manager._sync_live_board()

    rows = manager._live_board_rows()
    rendered = manager._render_live_board(rows)
    digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    message_id = _current_live_board_message_id(manager)
    if message_id is None:
        raise RuntimeError("Telegram live board has no stored message after normal sync")

    assert manager._bot_token is not None
    assert manager._destination_chat_id is not None

    manager._mark_board_attempt()
    try:
        _bot_api_call(
            manager._bot_token,
            "editMessageText",
            {
                "chat_id": manager._destination_chat_id,
                "message_id": message_id,
                "text": rendered,
                "disable_web_page_preview": "true",
            },
        )
    except TelegramPublishError as exc:
        if manager._is_missing_board_message(exc):
            manager._replace_missing_live_board(rendered, digest)
            return True
        if not _is_message_not_modified(exc):
            raise

    # Record the text/digest even when Telegram replied "message is not modified".
    # That response proves the stored message already has exactly the desired text.
    manager._record_board_message(
        message_id,
        rendered,
        digest,
        created=False,
    )

    # Re-pin the same board once at process startup. Unpinning this specific message
    # does not disturb any other group pins; re-pinning makes this live board the
    # current pinned selection in Telegram clients.
    try:
        _bot_api_call(
            manager._bot_token,
            "unpinChatMessage",
            {
                "chat_id": manager._destination_chat_id,
                "message_id": message_id,
            },
        )
    except TelegramPublishError as exc:
        if not _is_benign_unpin(exc):
            raise

    _bot_api_call(
        manager._bot_token,
        "pinChatMessage",
        {
            "chat_id": manager._destination_chat_id,
            "message_id": message_id,
            "disable_notification": "true",
        },
    )
    manager._record_board_pinned(message_id)
    return True


def reconcile_live_board_on_startup() -> bool:
    """Best-effort startup repair. Failure is logged and never blocks trading startup."""

    settings = get_publisher_settings()
    if (
        not settings.enabled
        or settings.bot_token is None
        or settings.destination_chat_id is None
    ):
        logger.info(
            "Telegram live board startup reconcile skipped: publisher is not configured"
        )
        return False

    manager = Day34CutoverTelegramPublisherManager(
        session_factory=get_session_factory(),
        enabled=settings.enabled,
        bot_token=settings.bot_token,
        destination_chat_id=settings.destination_chat_id,
        poll_seconds=settings.poll_seconds,
        reader_exclusion_active=True,
        reference_user_id=None,
    )

    try:
        repaired = force_reconcile_live_board(manager)
    except Exception as exc:
        code = (
            exc.code
            if isinstance(exc, TelegramPublishError)
            else "startup_live_board_reconcile_failed"
        )
        reason = exc.reason if isinstance(exc, TelegramPublishError) else str(exc)
        try:
            manager._record_board_failure(str(code), str(reason)[:500])
        except Exception:
            logger.exception("Could not record Telegram live board startup reconcile failure")
        logger.warning(
            "Telegram live board startup reconcile failed; application startup continues code=%s",
            code,
        )
        return False

    logger.info("Telegram live board reconciled and re-pinned from current trade state")
    return repaired


def main() -> None:
    reconcile_live_board_on_startup()


if __name__ == "__main__":
    main()
