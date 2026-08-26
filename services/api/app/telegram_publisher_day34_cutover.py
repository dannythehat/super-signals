"""Day 34 cutover-safe Telegram publication seeding.

This thin runtime layer keeps the full Day34 publisher implementation intact while
preventing deployment from replaying historical Day26-33 Signals, lifecycle events or
in-app alerts as if they were new. The pinned Live Trades Board is intentionally NOT
cutover-filtered: anything still broker-active at deployment belongs on the live board.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from app.telegram_publisher import PublicationAttempt, TelegramPublishError, _bot_api_call
from app.telegram_publisher_day20 import LifecyclePublicationAttempt
from app.telegram_publisher_day34 import Day34TelegramPublisherManager
from app.trade_identity import prefix_public_trade_identity, public_trade_identity

logger = logging.getLogger(__name__)

_BOARD_PIN_RETRY_BACKOFF = timedelta(minutes=1)


class Day34CutoverTelegramPublisherManager(Day34TelegramPublisherManager):
    """Day34 publisher with a persisted forward-only member-notification boundary."""

    async def _run(self) -> None:
        """Keep group logging alive without auxiliary Bot API checks blocking sends.

        ``Day19.start`` still performs and audits the full getMe/getChat/getChatMember
        connection verification at startup. Re-running those three metadata calls before
        every publisher cycle is not authoritative for delivery and can suppress an
        otherwise valid ``sendMessage`` without ever incrementing ``attempt_count``.

        The actual Telegram send is the only delivery authority after startup: _deliver
        records a concrete Telegram failure if it cannot post. The private-reader
        destination-collision check remains local/database-backed and is kept on every
        cycle. Trading is never called from this loop.
        """

        while not self._stop_event.is_set():
            try:
                if self._active_reader_source_collision():
                    logger.error(
                        "Telegram group logger cycle skipped: destination is an active reader source"
                    )
                else:
                    await asyncio.to_thread(self._seed_missing_publications)
                    attempt = await asyncio.to_thread(self._claim_next)
                    if attempt is not None:
                        await self._deliver(attempt)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A logger failure must be visible, but can never affect MT5 trading.
                logger.exception("Telegram group logger cycle failed; MT5 trading unchanged")

            if self._stop_event.is_set():
                break
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._poll_seconds,
                )
            except TimeoutError:
                continue

    def _claim_next(self) -> Any:
        """Attach the same public trade identity to every root and lifecycle post."""

        attempt = super()._claim_next()
        if attempt is None or not isinstance(attempt, PublicationAttempt):
            return attempt

        rendered = prefix_public_trade_identity(attempt.signal_id, attempt.text)
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE telegram_publications
                    SET rendered_text=:rendered_text,
                        updated_at=now()
                    WHERE id=:publication_id
                      AND status='sending'
                    """
                ),
                {
                    "publication_id": attempt.publication_id,
                    "rendered_text": rendered,
                },
            )
            session.commit()

        if isinstance(attempt, LifecyclePublicationAttempt):
            return LifecyclePublicationAttempt(
                publication_id=attempt.publication_id,
                signal_id=attempt.signal_id,
                text=rendered,
                reply_to_message_id=attempt.reply_to_message_id,
            )
        return PublicationAttempt(
            publication_id=attempt.publication_id,
            signal_id=attempt.signal_id,
            text=rendered,
        )

    @staticmethod
    def _render_live_board(rows: list[Any]) -> str:
        """Render every active Signal with its permanent provider-hidden identity."""

        open_count = sum(1 for row in rows if row["open_tp_indices"])
        pending_count = sum(1 for row in rows if row["pending_tp_indices"])
        lines = [
            "📌 SUPER SIGNALS · LIVE TRADES",
            f"OPEN {open_count} · PENDING {pending_count}",
        ]
        if not rows:
            lines.extend(["", "No active trades."])
            return "\n".join(lines)

        lines.append("")
        for row in rows:
            identity = public_trade_identity(row["signal_id"], row["member_trade_number"])
            symbol = str(row["symbol"] or "").upper()
            side = str(row["side"] or "").upper()
            open_indices = [int(value) for value in (row["open_tp_indices"] or [])]
            pending_indices = [int(value) for value in (row["pending_tp_indices"] or [])]
            states: list[str] = []
            if open_indices:
                states.append("/".join(f"TP{index}" for index in open_indices) + " open")
            if pending_indices:
                states.append("/".join(f"TP{index}" for index in pending_indices) + " pending")
            lines.append(
                f"{identity.label} · {symbol} {side} · {' · '.join(states)}"
            )
        return "\n".join(lines)

    def _seed_missing_publications(self) -> None:
        with self._session_factory() as session:
            publish_after = session.execute(
                text("SELECT publish_after FROM day34_summary_state WHERE id=1")
            ).scalar_one()

            # A canonical Signal can appear before Day26 finishes broker placement. It
            # is suppressed from the member feed until the success audit exists. Old
            # pre-cutover placement successes do not become new Day34 root posts.
            session.execute(
                text(
                    """
                    UPDATE telegram_publications AS pub
                    SET status='suppressed',
                        failure_code='day34_not_broker_placed',
                        failure_reason='Day 34 publishes roots only after confirmed broker placement.',
                        updated_at=now()
                    WHERE pub.publication_kind='signal_created'
                      AND pub.lifecycle_event_id IS NULL
                      AND pub.status='pending'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM audit_events AS placed
                          WHERE placed.entity_type='signal'
                            AND placed.entity_id=pub.signal_id
                            AND placed.event_type='mt5.day26_execution_success'
                            AND placed.created_at > :publish_after
                      )
                    """
                ),
                {"publish_after": publish_after},
            )

            # If the publisher polled between Signal creation and broker completion,
            # reactivate ONLY the row we suppressed for this Day34 reason and ONLY when
            # a post-cutover confirmed placement subsequently appears.
            session.execute(
                text(
                    """
                    UPDATE telegram_publications AS pub
                    SET status='pending',
                        failure_code=NULL,
                        failure_reason=NULL,
                        updated_at=now()
                    WHERE pub.publication_kind='signal_created'
                      AND pub.lifecycle_event_id IS NULL
                      AND pub.status='suppressed'
                      AND pub.failure_code='day34_not_broker_placed'
                      AND pub.telegram_message_id IS NULL
                      AND EXISTS (
                          SELECT 1
                          FROM audit_events AS placed
                          WHERE placed.entity_type='signal'
                            AND placed.entity_id=pub.signal_id
                            AND placed.event_type='mt5.day26_execution_success'
                            AND placed.created_at > :publish_after
                      )
                    """
                ),
                {"publish_after": publish_after},
            )

            session.execute(
                text(
                    """
                    INSERT INTO telegram_publications (
                        signal_id, publication_kind, status
                    )
                    SELECT sig.id, 'signal_created', 'pending'
                    FROM signals AS sig
                    WHERE EXISTS (
                        SELECT 1
                        FROM audit_events AS placed
                        WHERE placed.entity_type='signal'
                          AND placed.entity_id=sig.id
                          AND placed.event_type='mt5.day26_execution_success'
                          AND placed.created_at > :publish_after
                    )
                      AND NOT EXISTS (
                        SELECT 1
                        FROM telegram_publications AS pub
                        WHERE pub.signal_id=sig.id
                          AND pub.publication_kind='signal_created'
                          AND pub.lifecycle_event_id IS NULL
                      )
                    ON CONFLICT DO NOTHING
                    """
                ),
                {"publish_after": publish_after},
            )

            # Lifecycle events are forward-only as well. An older active trade may
            # still receive new management/settlement events after cutover, but those
            # can publish only if a historical root was already sent or this trade was
            # placed after cutover and therefore has a Day34 root path.
            session.execute(
                text(
                    """
                    INSERT INTO telegram_publications (
                        signal_id, lifecycle_event_id, publication_kind, status
                    )
                    SELECT ev.signal_id, ev.id, 'lifecycle_event', 'pending'
                    FROM signal_lifecycle_events AS ev
                    WHERE ev.occurred_at > :publish_after
                      AND (
                          EXISTS (
                              SELECT 1
                              FROM telegram_publications AS root
                              WHERE root.signal_id=ev.signal_id
                                AND root.publication_kind='signal_created'
                                AND root.lifecycle_event_id IS NULL
                                AND root.status='sent'
                                AND root.telegram_message_id IS NOT NULL
                          )
                          OR EXISTS (
                              SELECT 1
                              FROM audit_events AS placed
                              WHERE placed.entity_type='signal'
                                AND placed.entity_id=ev.signal_id
                                AND placed.event_type='mt5.day26_execution_success'
                                AND placed.created_at > :publish_after
                          )
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM telegram_publications AS pub
                          WHERE pub.lifecycle_event_id=ev.id
                      )
                    ON CONFLICT DO NOTHING
                    """
                ),
                {"publish_after": publish_after},
            )

            self._seed_in_app_notifications_after_cutover(
                session,
                publish_after=publish_after,
            )
            session.commit()

        if self._summary_service is not None:
            self._summary_service.seed_due()
        self._seed_summary_deliveries()

        # Intentionally sees ALL current broker-active state, including a trade opened
        # before deployment that is still live after deployment.
        self._sync_live_board_safely()

    def _sync_live_board(self) -> None:
        """Edit trade-state changes immediately, but rate-limit pin-only failures.

        A missing Telegram pin permission is operationally harmless but previously made
        the publisher retry every poll. We still edit the board immediately whenever its
        broker-backed digest changes; only an unchanged board whose sole outstanding work
        is a recently failed pin receives this one-minute backoff.

        Telegram can also migrate a basic group to a supergroup. In that case the old
        board message ID may no longer exist in the new chat. Only that explicit
        message-not-found condition is allowed to create one replacement board; all
        other failures remain fail-safe and cannot create duplicates.
        """
        rows = self._live_board_rows()
        rendered = self._render_live_board(rows)
        desired_digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        with self._session_factory() as session:
            state = session.execute(
                text(
                    """
                    SELECT telegram_message_id, source_digest, pinned_at,
                           failure_code, attempted_at
                    FROM telegram_live_board_state
                    WHERE id=1
                    """
                )
            ).mappings().one()

        digest_unchanged = str(state["source_digest"] or "") == desired_digest
        pin_only_pending = (
            state["telegram_message_id"] is not None
            and state["pinned_at"] is None
            and digest_unchanged
            and str(state["failure_code"] or "") == "telegram_http_400"
        )
        attempted_at = state["attempted_at"]
        if pin_only_pending and isinstance(attempted_at, datetime):
            now = datetime.now(UTC)
            attempt = attempted_at if attempted_at.tzinfo else attempted_at.replace(tzinfo=UTC)
            if now - attempt < _BOARD_PIN_RETRY_BACKOFF:
                return

        try:
            super()._sync_live_board()
        except TelegramPublishError as exc:
            if not self._is_missing_board_message(exc):
                raise
            self._replace_missing_live_board(rendered, desired_digest)

    @staticmethod
    def _is_missing_board_message(exc: TelegramPublishError) -> bool:
        if exc.code != "telegram_http_400":
            return False
        reason = exc.reason.lower()
        return (
            "message to pin not found" in reason
            or "message to edit not found" in reason
            or "message not found" in reason
        )

    def _replace_missing_live_board(self, rendered: str, digest: str) -> None:
        """Create one new board only after Telegram proves the stored one is gone."""
        assert self._bot_token is not None
        assert self._destination_chat_id is not None

        result = _bot_api_call(
            self._bot_token,
            "sendMessage",
            {
                "chat_id": self._destination_chat_id,
                "text": rendered,
                "disable_web_page_preview": "true",
            },
        )
        replacement_id = int(result["message_id"])
        self._record_board_message(
            replacement_id,
            rendered,
            digest,
            created=True,
        )
        _bot_api_call(
            self._bot_token,
            "pinChatMessage",
            {
                "chat_id": self._destination_chat_id,
                "message_id": replacement_id,
                "disable_notification": "true",
            },
        )
        self._record_board_pinned(replacement_id)

    @staticmethod
    def _seed_in_app_notifications_after_cutover(
        session: Any,
        *,
        publish_after: Any,
    ) -> None:
        session.execute(
            text(
                """
                INSERT INTO notification_events (
                    event_key, signal_id, lifecycle_event_id, user_id,
                    audience, kind, title, body, payload
                )
                SELECT
                    'signal-open:' || sig.id::text,
                    sig.id,
                    NULL,
                    NULL,
                    'shared',
                    'trade_open',
                    'SS-' || upper(left(replace(sig.id::text,'-',''),10)) || ' · ' ||
                        COALESCE(sig.symbol,'') || ' ' || COALESCE(sig.side,'') || ' opened',
                    'Trade placed and confirmed at the broker.',
                    jsonb_build_object(
                        'broker_confirmed', true,
                        'public_trade_reference', 'SS-' || upper(left(replace(sig.id::text,'-',''),10)),
                        'provider_identity_exposed', false,
                        'trade_action_created', false
                    )
                FROM signals AS sig
                WHERE EXISTS (
                    SELECT 1
                    FROM audit_events AS placed
                    WHERE placed.entity_type='signal'
                      AND placed.entity_id=sig.id
                      AND placed.event_type='mt5.day26_execution_success'
                      AND placed.created_at > :publish_after
                )
                ON CONFLICT (event_key) DO NOTHING
                """
            ),
            {"publish_after": publish_after},
        )

        session.execute(
            text(
                """
                INSERT INTO notification_events (
                    event_key, signal_id, lifecycle_event_id, user_id,
                    audience, kind, title, body, payload
                )
                SELECT
                    'lifecycle:' || ev.id::text,
                    ev.signal_id,
                    ev.id,
                    NULL,
                    'shared',
                    CASE
                        WHEN ev.event_type LIKE 'broker_result_%' THEN 'trade_result'
                        ELSE 'trade_update'
                    END,
                    'SS-' || upper(left(replace(ev.signal_id::text,'-',''),10)) || ' · ' ||
                    CASE
                        WHEN ev.event_type='broker_result_win' THEN 'Trade won'
                        WHEN ev.event_type='broker_result_loss' THEN 'Trade lost'
                        WHEN ev.event_type='broker_result_breakeven' THEN 'Trade closed at break even'
                        WHEN ev.event_type='broker_result_closed' THEN 'Trade closed'
                        ELSE 'Trade update'
                    END,
                    'SS-' || upper(left(replace(ev.signal_id::text,'-',''),10)) || ' · ' || ev.rendered_text,
                    jsonb_build_object(
                        'origin', ev.origin,
                        'broker_result', ev.event_type LIKE 'broker_result_%',
                        'public_trade_reference', 'SS-' || upper(left(replace(ev.signal_id::text,'-',''),10)),
                        'provider_identity_exposed', false,
                        'trade_action_created', false
                    )
                FROM signal_lifecycle_events AS ev
                WHERE ev.occurred_at > :publish_after
                  AND (
                      EXISTS (
                          SELECT 1
                          FROM telegram_publications AS root
                          WHERE root.signal_id=ev.signal_id
                            AND root.publication_kind='signal_created'
                            AND root.lifecycle_event_id IS NULL
                            AND root.status='sent'
                            AND root.telegram_message_id IS NOT NULL
                      )
                      OR EXISTS (
                          SELECT 1
                          FROM audit_events AS placed
                          WHERE placed.entity_type='signal'
                            AND placed.entity_id=ev.signal_id
                            AND placed.event_type='mt5.day26_execution_success'
                            AND placed.created_at > :publish_after
                      )
                  )
                ON CONFLICT (event_key) DO NOTHING
                """
            ),
            {"publish_after": publish_after},
        )


__all__ = ["Day34CutoverTelegramPublisherManager"]
