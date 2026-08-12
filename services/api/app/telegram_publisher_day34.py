"""Day 34 Telegram feed: execution-first roots, lifecycle replies and one pinned board.

The broker/database state is authoritative. Canonical Signals that never reached a
confirmed Day 26 broker placement are suppressed from the member feed. The pinned Live
Trades Board is one bot-authored message that is edited in place and never affects
trading if Telegram is unavailable.
"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import text

from app.models import AuditEvent
from app.telegram_publisher import TelegramPublishError, _bot_api_call
from app.telegram_publisher_day20 import Day20TelegramPublisherManager

DAY34_PUBLISHER_VERSION = "day34-publisher-v1"


class Day34TelegramPublisherManager(Day20TelegramPublisherManager):
    """Day 20 threaded history with Day 34 broker-first visibility and live board."""

    def _seed_missing_publications(self) -> None:
        """Seed only Signals with a completed broker placement acceptance event."""
        with self._session_factory() as session:
            # Retrofit old Day 19/20 behaviour: a canonical Signal is not enough to
            # become member-visible. Any still-pending pre-Day34 root without a
            # confirmed Day 26 placement is permanently suppressed.
            session.execute(
                text(
                    """
                    UPDATE telegram_publications AS pub
                    SET status = 'suppressed',
                        failure_code = 'day34_not_broker_placed',
                        failure_reason = 'Day 34 publishes roots only after confirmed broker placement.',
                        updated_at = now()
                    WHERE pub.publication_kind = 'signal_created'
                      AND pub.lifecycle_event_id IS NULL
                      AND pub.status = 'pending'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM audit_events AS placed
                          WHERE placed.entity_type = 'signal'
                            AND placed.entity_id = pub.signal_id
                            AND placed.event_type = 'mt5.day26_execution_success'
                      )
                    """
                )
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
                        WHERE placed.entity_type = 'signal'
                          AND placed.entity_id = sig.id
                          AND placed.event_type = 'mt5.day26_execution_success'
                    )
                      AND NOT EXISTS (
                        SELECT 1
                        FROM telegram_publications AS pub
                        WHERE pub.signal_id = sig.id
                          AND pub.publication_kind = 'signal_created'
                          AND pub.lifecycle_event_id IS NULL
                      )
                    ON CONFLICT DO NOTHING
                    """
                )
            )

            # A lifecycle reply is member-visible only when its Signal has a real
            # broker-placed root. This keeps skipped/rejected canonical evidence in
            # PostgreSQL without leaking it into the member feed.
            session.execute(
                text(
                    """
                    INSERT INTO telegram_publications (
                        signal_id, lifecycle_event_id, publication_kind, status
                    )
                    SELECT ev.signal_id, ev.id, 'lifecycle_event', 'pending'
                    FROM signal_lifecycle_events AS ev
                    WHERE EXISTS (
                        SELECT 1
                        FROM audit_events AS placed
                        WHERE placed.entity_type = 'signal'
                          AND placed.entity_id = ev.signal_id
                          AND placed.event_type = 'mt5.day26_execution_success'
                    )
                      AND NOT EXISTS (
                        SELECT 1
                        FROM telegram_publications AS pub
                        WHERE pub.lifecycle_event_id = ev.id
                      )
                    ON CONFLICT DO NOTHING
                    """
                )
            )

            self._seed_in_app_notifications(session)
            session.commit()

        # Board failure is deliberately isolated from root/lifecycle publication and
        # from trading. The same state row/message ID is reused on every retry.
        self._sync_live_board_safely()

    @staticmethod
    def _seed_in_app_notifications(session: Any) -> None:
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
                    COALESCE(sig.symbol,'') || ' ' || COALESCE(sig.side,'') || ' opened',
                    'Trade placed and confirmed at the broker.',
                    jsonb_build_object(
                        'broker_confirmed', true,
                        'provider_identity_exposed', false,
                        'trade_action_created', false
                    )
                FROM signals AS sig
                WHERE EXISTS (
                    SELECT 1 FROM audit_events AS placed
                    WHERE placed.entity_type='signal'
                      AND placed.entity_id=sig.id
                      AND placed.event_type='mt5.day26_execution_success'
                )
                ON CONFLICT (event_key) DO NOTHING
                """
            )
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
                    CASE
                        WHEN ev.event_type = 'broker_result_win' THEN 'Trade won'
                        WHEN ev.event_type = 'broker_result_loss' THEN 'Trade lost'
                        WHEN ev.event_type = 'broker_result_breakeven' THEN 'Trade closed at break even'
                        WHEN ev.event_type = 'broker_result_closed' THEN 'Trade closed'
                        ELSE 'Trade update'
                    END,
                    ev.rendered_text,
                    jsonb_build_object(
                        'origin', ev.origin,
                        'broker_result', ev.event_type LIKE 'broker_result_%',
                        'provider_identity_exposed', false,
                        'trade_action_created', false
                    )
                FROM signal_lifecycle_events AS ev
                WHERE EXISTS (
                    SELECT 1 FROM audit_events AS placed
                    WHERE placed.entity_type='signal'
                      AND placed.entity_id=ev.signal_id
                      AND placed.event_type='mt5.day26_execution_success'
                )
                ON CONFLICT (event_key) DO NOTHING
                """
            )
        )

    def _sync_live_board_safely(self) -> None:
        if not self.configured:
            return
        try:
            self._sync_live_board()
        except Exception as exc:
            # No board exception may escape into the publisher loop or trading path.
            code = exc.code if isinstance(exc, TelegramPublishError) else "day34_live_board_failure"
            reason = exc.reason if isinstance(exc, TelegramPublishError) else str(exc)
            self._record_board_failure(str(code), str(reason)[:500])

    def _sync_live_board(self) -> None:
        assert self._bot_token is not None
        assert self._destination_chat_id is not None

        rows = self._live_board_rows()
        rendered = self._render_live_board(rows)
        digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        with self._session_factory() as session:
            state = session.execute(
                text(
                    """
                    SELECT telegram_message_id, source_digest, pinned_at
                    FROM telegram_live_board_state
                    WHERE id = 1
                    """
                )
            ).mappings().one()

        message_id = (
            int(state["telegram_message_id"])
            if state["telegram_message_id"] is not None
            else None
        )
        digest_changed = str(state["source_digest"] or "") != digest
        needs_pin = state["pinned_at"] is None
        if message_id is not None and not digest_changed and not needs_pin:
            return

        self._mark_board_attempt()
        if message_id is None:
            result = _bot_api_call(
                self._bot_token,
                "sendMessage",
                {
                    "chat_id": self._destination_chat_id,
                    "text": rendered,
                    "disable_web_page_preview": "true",
                },
            )
            message_id = int(result["message_id"])
            self._record_board_message(message_id, rendered, digest, created=True)
            needs_pin = True
        elif digest_changed:
            _bot_api_call(
                self._bot_token,
                "editMessageText",
                {
                    "chat_id": self._destination_chat_id,
                    "message_id": message_id,
                    "text": rendered,
                    "disable_web_page_preview": "true",
                },
            )
            self._record_board_message(message_id, rendered, digest, created=False)

        if needs_pin:
            _bot_api_call(
                self._bot_token,
                "pinChatMessage",
                {
                    "chat_id": self._destination_chat_id,
                    "message_id": message_id,
                    "disable_notification": "true",
                },
            )
            self._record_board_pinned(message_id)

    def _live_board_rows(self) -> list[Any]:
        """One provider-hidden row per effectively active canonical Signal."""
        with self._session_factory() as session:
            return list(
                session.execute(
                    text(
                        """
                        WITH leg_state AS (
                            SELECT
                                s.id AS signal_id,
                                COALESCE(s.symbol,'') AS symbol,
                                COALESCE(s.side,'') AS side,
                                p.tp_index,
                                CASE
                                    WHEN o.status IN ('won','lost','breakeven','closed_unknown') THEN 'closed'
                                    WHEN o.status = 'pending' THEN 'pending'
                                    WHEN p.status = 'open' AND p.broker_position_id IS NOT NULL THEN 'open'
                                    ELSE 'closed'
                                END AS effective_status
                            FROM positions AS p
                            JOIN signals AS s ON s.id = p.signal_id
                            LEFT JOIN performance_trade_outcomes AS o ON o.position_id = p.id
                            WHERE EXISTS (
                                SELECT 1
                                FROM audit_events AS placed
                                WHERE placed.entity_type='signal'
                                  AND placed.entity_id=s.id
                                  AND placed.event_type='mt5.day26_execution_success'
                            )
                        )
                        SELECT
                            signal_id,
                            MAX(symbol) AS symbol,
                            MAX(side) AS side,
                            ARRAY_AGG(DISTINCT tp_index ORDER BY tp_index)
                                FILTER (WHERE effective_status='open') AS open_tp_indices,
                            ARRAY_AGG(DISTINCT tp_index ORDER BY tp_index)
                                FILTER (WHERE effective_status='pending') AS pending_tp_indices
                        FROM leg_state
                        GROUP BY signal_id
                        HAVING BOOL_OR(effective_status='open') OR BOOL_OR(effective_status='pending')
                        ORDER BY signal_id
                        """
                    )
                ).mappings().all()
            )

    @staticmethod
    def _render_live_board(rows: list[Any]) -> str:
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
            symbol = str(row["symbol"] or "").upper()
            side = str(row["side"] or "").upper()
            open_indices = [int(value) for value in (row["open_tp_indices"] or [])]
            pending_indices = [int(value) for value in (row["pending_tp_indices"] or [])]
            states: list[str] = []
            if open_indices:
                states.append("/".join(f"TP{index}" for index in open_indices) + " open")
            if pending_indices:
                states.append("/".join(f"TP{index}" for index in pending_indices) + " pending")
            lines.append(f"{symbol} {side} · {' · '.join(states)}")
        return "\n".join(lines)

    def _mark_board_attempt(self) -> None:
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE telegram_live_board_state
                    SET status='sending',
                        attempt_count=attempt_count+1,
                        attempted_at=now(),
                        failure_code=NULL,
                        failure_reason=NULL,
                        updated_at=now()
                    WHERE id=1
                    """
                )
            )
            session.commit()

    def _record_board_message(
        self,
        message_id: int,
        rendered: str,
        digest: str,
        *,
        created: bool,
    ) -> None:
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE telegram_live_board_state
                    SET destination_chat_id=:chat_id,
                        telegram_message_id=:message_id,
                        rendered_text=:rendered,
                        source_digest=:digest,
                        status='ready',
                        failure_code=NULL,
                        failure_reason=NULL,
                        updated_at=now()
                    WHERE id=1
                    """
                ),
                {
                    "chat_id": self._destination_chat_id,
                    "message_id": message_id,
                    "rendered": rendered,
                    "digest": digest,
                },
            )
            session.add(
                AuditEvent(
                    actor_user_id=None,
                    event_type="telegram.day34_live_board_updated",
                    entity_type="telegram_live_board",
                    entity_id=None,
                    payload={
                        "publisher_version": DAY34_PUBLISHER_VERSION,
                        "telegram_message_id": message_id,
                        "board_message_created": created,
                        "same_message_reused": not created,
                        "provider_identity_exposed": False,
                        "private_balance_exposed": False,
                        "trade_action_created": False,
                    },
                )
            )
            session.commit()

    def _record_board_pinned(self, message_id: int) -> None:
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE telegram_live_board_state
                    SET pinned_at=COALESCE(pinned_at, now()), status='ready', updated_at=now()
                    WHERE id=1 AND telegram_message_id=:message_id
                    """
                ),
                {"message_id": message_id},
            )
            session.add(
                AuditEvent(
                    actor_user_id=None,
                    event_type="telegram.day34_live_board_pinned",
                    entity_type="telegram_live_board",
                    entity_id=None,
                    payload={
                        "publisher_version": DAY34_PUBLISHER_VERSION,
                        "telegram_message_id": message_id,
                        "trade_action_created": False,
                    },
                )
            )
            session.commit()

    def _record_board_failure(self, code: str, reason: str) -> None:
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE telegram_live_board_state
                    SET status='failed', failure_code=:code, failure_reason=:reason,
                        updated_at=now()
                    WHERE id=1
                    """
                ),
                {"code": code[:80], "reason": reason[:500]},
            )
            session.add(
                AuditEvent(
                    actor_user_id=None,
                    event_type="telegram.day34_live_board_failed",
                    entity_type="telegram_live_board",
                    entity_id=None,
                    payload={
                        "publisher_version": DAY34_PUBLISHER_VERSION,
                        "failure_code": code[:80],
                        "trading_unchanged": True,
                        "retry_idempotent": True,
                        "trade_action_created": False,
                    },
                )
            )
            session.commit()


__all__ = ["Day34TelegramPublisherManager"]
