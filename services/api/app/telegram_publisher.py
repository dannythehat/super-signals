"""Day 19 publish-only Telegram mirror.

This module uses a Bot API token that is separate from every private reader session.
Database events are committed first; Telegram delivery is a later best-effort mirror.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.models import AuditEvent

PUBLISHER_VERSION = "day19-publisher-v1"

# Telegram upgrades basic groups to supergroups by assigning a new chat ID. The Bot
# API returns that authoritative ID in parameters.migrate_to_chat_id. Keep a
# process-local map so every publishing path automatically follows the migration.
_MIGRATED_CHAT_IDS: dict[int, int] = {}


@dataclass(frozen=True, slots=True)
class PublisherConnectionStatus:
    configured: bool
    enabled: bool
    destination_chat_type: str | None
    bot_membership_status: str | None
    minimum_permissions_ok: bool
    source_collision: bool
    reason: str


@dataclass(frozen=True, slots=True)
class PublicationAttempt:
    publication_id: UUID
    signal_id: UUID
    text: str


class TelegramPublishError(RuntimeError):
    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _decimal_text(value: Any) -> str:
    decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    normalized = decimal_value.normalize()
    return format(normalized, "f")


def render_signal_post(row: Any) -> str:
    """Render canonical fields only; never use source/provider wording or identity."""

    lines = [
        "SUPER SIGNALS",
        "",
        f"{str(row['symbol']).upper()} {str(row['side']).upper()}",
        f"Entry: {_decimal_text(row['entry_price'])}",
        f"Stop Loss: {_decimal_text(row['stop_loss'])}",
    ]
    take_profits = list(row["take_profits"] or [])
    for index, target in enumerate(take_profits, start=1):
        lines.append(f"TP{index}: {_decimal_text(target)}")
    # A provider OPEN target is a real broker-mapped runner, not decorative metadata.
    # Keep it visible in the root post so a member never sees every numeric TP settle
    # and reasonably concludes the whole trade is finished while the runner remains.
    if bool(row.get("has_open_runner", False)):
        lines.append(f"TP{len(take_profits) + 1}: OPEN")
    multiplier = Decimal(str(row["risk_multiplier"]))
    lines.append("Size: Double" if multiplier == Decimal("2") else "Size: Standard")
    return "\n".join(lines)


def _resolved_bot_chat_id(chat_id: int) -> int:
    """Return Telegram's current authoritative chat ID after any known migration."""
    resolved = int(chat_id)
    visited: set[int] = set()
    while resolved in _MIGRATED_CHAT_IDS and resolved not in visited:
        visited.add(resolved)
        resolved = int(_MIGRATED_CHAT_IDS[resolved])
    return resolved


def _telegram_migration_target(response_body: Any) -> int | None:
    if not isinstance(response_body, dict):
        return None
    parameters = response_body.get("parameters")
    if not isinstance(parameters, dict):
        return None
    raw = parameters.get("migrate_to_chat_id")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _bot_api_call(
    token: str,
    method: str,
    payload: dict[str, Any],
    *,
    _allow_migration_retry: bool = True,
) -> dict[str, Any]:
    effective_payload = dict(payload)
    original_chat_id: int | None = None
    if "chat_id" in effective_payload:
        try:
            original_chat_id = int(effective_payload["chat_id"])
        except (TypeError, ValueError):
            original_chat_id = None
        if original_chat_id is not None:
            effective_payload["chat_id"] = _resolved_bot_chat_id(original_chat_id)

    body = urlencode(effective_payload).encode("utf-8")
    request = Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=12) as response:  # noqa: S310 - fixed Telegram API host
            parsed = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            payload_body = json.loads(exc.read().decode("utf-8"))
            description = str(payload_body.get("description") or "Telegram rejected the request.")
            error_code = str(payload_body.get("error_code") or exc.code)
            migration_target = _telegram_migration_target(payload_body)
        except Exception:
            payload_body = None
            description = "Telegram rejected the request."
            error_code = str(exc.code)
            migration_target = None

        if (
            _allow_migration_retry
            and migration_target is not None
            and original_chat_id is not None
        ):
            old_chat_id = int(effective_payload.get("chat_id", original_chat_id))
            _MIGRATED_CHAT_IDS[old_chat_id] = migration_target
            _MIGRATED_CHAT_IDS[original_chat_id] = migration_target
            retry_payload = dict(payload)
            retry_payload["chat_id"] = migration_target
            return _bot_api_call(
                token,
                method,
                retry_payload,
                _allow_migration_retry=False,
            )

        raise TelegramPublishError(f"telegram_http_{error_code}", description[:300]) from None
    except (URLError, TimeoutError, OSError):
        raise TelegramPublishError(
            "telegram_transport_failure",
            "Telegram could not be reached; delivery was not retried automatically.",
        ) from None
    except (ValueError, TypeError):
        raise TelegramPublishError(
            "telegram_invalid_response",
            "Telegram returned an unreadable response; delivery was not retried automatically.",
        ) from None

    if not parsed.get("ok"):
        migration_target = _telegram_migration_target(parsed)
        if (
            _allow_migration_retry
            and migration_target is not None
            and original_chat_id is not None
        ):
            old_chat_id = int(effective_payload.get("chat_id", original_chat_id))
            _MIGRATED_CHAT_IDS[old_chat_id] = migration_target
            _MIGRATED_CHAT_IDS[original_chat_id] = migration_target
            retry_payload = dict(payload)
            retry_payload["chat_id"] = migration_target
            return _bot_api_call(
                token,
                method,
                retry_payload,
                _allow_migration_retry=False,
            )
        raise TelegramPublishError(
            f"telegram_api_{parsed.get('error_code', 'error')}",
            str(parsed.get("description") or "Telegram rejected the request.")[:300],
        )
    result = parsed.get("result")
    return result if isinstance(result, dict) else {"value": result}


class TelegramPublisherManager:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        enabled: bool,
        bot_token: str | None,
        destination_chat_id: int | None,
        poll_seconds: int = 3,
    ) -> None:
        self._session_factory = session_factory
        self._enabled = enabled
        self._bot_token = bot_token
        self._destination_chat_id = destination_chat_id
        self._poll_seconds = poll_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    @property
    def configured(self) -> bool:
        return bool(self._bot_token and self._destination_chat_id is not None)

    async def start(self) -> None:
        if not self._enabled or not self.configured:
            return
        await asyncio.to_thread(self._mark_stale_sending_failed)
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="telegram-publisher")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                connection = await asyncio.to_thread(self.check_connection)
                if connection.minimum_permissions_ok and not connection.source_collision:
                    await asyncio.to_thread(self._seed_missing_publications)
                    attempt = await asyncio.to_thread(self._claim_next)
                    if attempt is not None:
                        await self._deliver(attempt)
            except Exception:
                # Publishing must never stop ingestion or application health.
                pass
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                continue

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

        try:
            me = _bot_api_call(self._bot_token, "getMe", {})
            chat = _bot_api_call(
                self._bot_token,
                "getChat",
                {"chat_id": self._destination_chat_id},
            )
            self._destination_chat_id = _resolved_bot_chat_id(self._destination_chat_id)
            membership = _bot_api_call(
                self._bot_token,
                "getChatMember",
                {"chat_id": self._destination_chat_id, "user_id": int(me["id"])},
            )
        except (TelegramPublishError, KeyError, TypeError, ValueError) as exc:
            reason = exc.reason if isinstance(exc, TelegramPublishError) else "Telegram bot identity could not be verified."
            return PublisherConnectionStatus(
                configured=True,
                enabled=True,
                destination_chat_type=None,
                bot_membership_status=None,
                minimum_permissions_ok=False,
                source_collision=False,
                reason=reason,
            )

        with self._session_factory() as session:
            source_collision = bool(
                session.execute(
                    text(
                        """
                        SELECT 1 FROM sources
                        WHERE chat_id = :chat_id
                          AND status <> 'revoked'
                        LIMIT 1
                        """
                    ),
                    {"chat_id": self._destination_chat_id},
                ).scalar_one_or_none()
            )
        if source_collision:
            return PublisherConnectionStatus(
                configured=True,
                enabled=True,
                destination_chat_type=None,
                bot_membership_status=None,
                minimum_permissions_ok=False,
                source_collision=True,
                reason="Publishing destination cannot also be an active reader source.",
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
            "can_manage_chat",
            "can_manage_topics",
            "can_edit_messages",
            "can_manage_direct_messages",
        )
        return not any(bool(membership.get(name)) for name in broad_permissions)

    def _seed_missing_publications(self) -> None:
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    INSERT INTO telegram_publications (
                        signal_id,
                        publication_kind,
                        status
                    )
                    SELECT sig.id, 'signal_created', 'pending'
                    FROM signals AS sig
                    LEFT JOIN telegram_publications AS pub
                      ON pub.signal_id = sig.id
                     AND pub.publication_kind = 'signal_created'
                    WHERE pub.id IS NULL
                    ON CONFLICT (signal_id, publication_kind) DO NOTHING
                    """
                )
            )
            session.commit()

    def _mark_stale_sending_failed(self) -> None:
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE telegram_publications
                    SET status = 'failed',
                        failure_code = 'delivery_state_uncertain',
                        failure_reason = 'A previous process stopped while delivery was in progress; automatic retry is disabled to avoid duplicate posts.',
                        updated_at = now()
                    WHERE status = 'sending'
                    """
                )
            )
            session.commit()

    def _claim_next(self) -> PublicationAttempt | None:
        assert self._destination_chat_id is not None
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT
                        pub.id AS publication_id,
                        pub.signal_id,
                        sig.symbol,
                        sig.side,
                        sig.entry_low AS entry_price,
                        sig.stop_loss,
                        sig.take_profits,
                        sig.has_open_runner,
                        sig.risk_multiplier
                    FROM telegram_publications AS pub
                    JOIN signals AS sig ON sig.id = pub.signal_id
                    WHERE pub.status = 'pending'
                      AND pub.publication_kind = 'signal_created'
                    ORDER BY pub.created_at ASC
                    FOR UPDATE OF pub SKIP LOCKED
                    LIMIT 1
                    """
                )
            ).mappings().first()
            if row is None:
                session.rollback()
                return None
            rendered = render_signal_post(row)
            session.execute(
                text(
                    """
                    UPDATE telegram_publications
                    SET status = 'sending',
                        rendered_text = :rendered_text,
                        destination_chat_id = :destination_chat_id,
                        attempt_count = attempt_count + 1,
                        attempted_at = now(),
                        failure_code = NULL,
                        failure_reason = NULL,
                        updated_at = now()
                    WHERE id = :publication_id
                      AND status = 'pending'
                    """
                ),
                {
                    "publication_id": row["publication_id"],
                    "rendered_text": rendered,
                    "destination_chat_id": self._destination_chat_id,
                },
            )
            session.commit()
            return PublicationAttempt(
                publication_id=row["publication_id"],
                signal_id=row["signal_id"],
                text=rendered,
            )

    async def _deliver(self, attempt: PublicationAttempt) -> None:
        assert self._bot_token is not None
        assert self._destination_chat_id is not None
        try:
            result = await asyncio.to_thread(
                _bot_api_call,
                self._bot_token,
                "sendMessage",
                {
                    "chat_id": self._destination_chat_id,
                    "text": attempt.text,
                    "disable_web_page_preview": "true",
                },
            )
            telegram_message_id = int(result["message_id"])
        except (TelegramPublishError, KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, TelegramPublishError):
                code, reason = exc.code, exc.reason
            else:
                code, reason = "telegram_invalid_success_response", "Telegram did not return a usable destination message ID."
            await asyncio.to_thread(self._record_failure, attempt, code, reason)
            return
        await asyncio.to_thread(self._record_success, attempt, telegram_message_id)

    def _record_success(self, attempt: PublicationAttempt, telegram_message_id: int) -> None:
        with self._session_factory() as session:
            updated = session.execute(
                text(
                    """
                    UPDATE telegram_publications
                    SET status = 'sent',
                        telegram_message_id = :telegram_message_id,
                        sent_at = now(),
                        updated_at = now()
                    WHERE id = :publication_id
                      AND status = 'sending'
                    RETURNING id
                    """
                ),
                {
                    "publication_id": attempt.publication_id,
                    "telegram_message_id": telegram_message_id,
                },
            ).scalar_one_or_none()
            if updated is not None:
                session.add(
                    AuditEvent(
                        actor_user_id=None,
                        event_type="telegram.publication_sent",
                        entity_type="signal",
                        entity_id=attempt.signal_id,
                        payload={
                            "publisher_version": PUBLISHER_VERSION,
                            "publication_id": str(attempt.publication_id),
                            "provider_identity_exposed": False,
                            "reader_session_used": False,
                            "position_created": False,
                            "trade_action_created": False,
                        },
                    )
                )
            session.commit()

    def _record_failure(
        self,
        attempt: PublicationAttempt,
        failure_code: str,
        failure_reason: str,
    ) -> None:
        with self._session_factory() as session:
            updated = session.execute(
                text(
                    """
                    UPDATE telegram_publications
                    SET status = 'failed',
                        failure_code = :failure_code,
                        failure_reason = :failure_reason,
                        updated_at = now()
                    WHERE id = :publication_id
                      AND status = 'sending'
                    RETURNING id
                    """
                ),
                {
                    "publication_id": attempt.publication_id,
                    "failure_code": failure_code[:80],
                    "failure_reason": failure_reason[:500],
                },
            ).scalar_one_or_none()
            if updated is not None:
                session.add(
                    AuditEvent(
                        actor_user_id=None,
                        event_type="telegram.publication_failed",
                        entity_type="signal",
                        entity_id=attempt.signal_id,
                        payload={
                            "publisher_version": PUBLISHER_VERSION,
                            "publication_id": str(attempt.publication_id),
                            "failure_code": failure_code[:80],
                            "canonical_signal_unchanged": True,
                            "reader_session_used": False,
                            "position_created": False,
                            "trade_action_created": False,
                        },
                    )
                )
            session.commit()