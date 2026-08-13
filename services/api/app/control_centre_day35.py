"""Day 35 Owner/Trading Admin control-centre read model.

The control centre is observability only. It reads canonical PostgreSQL state and never
places, closes, modifies or cancels a broker trade. Dangerous actions remain separate,
explicitly confirmed endpoints with their own audit contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

ControlTone = Literal["healthy", "attention", "critical", "neutral"]


@dataclass(frozen=True, slots=True)
class Day35ReviewStage:
    stage: str
    open_count: int
    oldest_at: datetime | None
    newest_at: datetime | None


@dataclass(frozen=True, slots=True)
class Day35AttentionItem:
    key: str
    tone: ControlTone
    title: str
    detail: str
    count: int
    area: str


@dataclass(frozen=True, slots=True)
class Day35RecentEvent:
    event_type: str
    entity_type: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Day35ControlCentreView:
    generated_at: datetime
    overall_status: ControlTone
    open_signals: int
    pending_signals: int
    trading_active_users: int
    trading_stopped_users: int
    source_live: int
    source_testing: int
    source_paused: int
    source_revoked: int
    telegram_connected: int
    telegram_attention: int
    mt5_connected: int
    mt5_attention: int
    active_users: int
    invited_users: int
    suspended_users: int
    revoked_users: int
    review_open: int
    review_open_24h: int
    message_errors_24h: int
    publication_failed: int
    publication_pending: int
    push_failed_24h: int
    telegram_notification_failed_24h: int
    live_board_ready: bool
    live_board_pinned: bool
    review_stages: tuple[Day35ReviewStage, ...]
    attention: tuple[Day35AttentionItem, ...]
    recent_events: tuple[Day35RecentEvent, ...]
    broker_trade_action_created: bool = False


class Day35ControlCentreService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def read(self) -> Day35ControlCentreView:
        now = datetime.now(UTC)
        with self._session_factory() as session:
            counts = session.execute(
                text(
                    """
                    SELECT
                        (SELECT COUNT(DISTINCT signal_id)::int FROM performance_trade_outcomes WHERE status='open') AS open_signals,
                        (SELECT COUNT(DISTINCT signal_id)::int FROM performance_trade_outcomes WHERE status='pending') AS pending_signals,
                        (SELECT COUNT(*)::int FROM user_trading_controls WHERE trading_status='active') AS trading_active_users,
                        (SELECT COUNT(*)::int FROM user_trading_controls WHERE trading_status<>'active') AS trading_stopped_users,
                        (SELECT COUNT(*)::int FROM sources WHERE status='live') AS source_live,
                        (SELECT COUNT(*)::int FROM sources WHERE status='testing') AS source_testing,
                        (SELECT COUNT(*)::int FROM sources WHERE status='paused') AS source_paused,
                        (SELECT COUNT(*)::int FROM sources WHERE status='revoked') AS source_revoked,
                        (SELECT COUNT(*)::int FROM telegram_accounts WHERE status='connected') AS telegram_connected,
                        (SELECT COUNT(*)::int FROM telegram_accounts WHERE status NOT IN ('connected','revoked')) AS telegram_attention,
                        (SELECT COUNT(*)::int FROM mt5_accounts WHERE status='connected') AS mt5_connected,
                        (SELECT COUNT(*)::int FROM mt5_accounts WHERE status NOT IN ('connected','revoked')) AS mt5_attention,
                        (SELECT COUNT(*)::int FROM users WHERE status='active') AS active_users,
                        (SELECT COUNT(*)::int FROM users WHERE status='invited') AS invited_users,
                        (SELECT COUNT(*)::int FROM users WHERE status='suspended') AS suspended_users,
                        (SELECT COUNT(*)::int FROM users WHERE status='revoked') AS revoked_users,
                        (SELECT COUNT(*)::int FROM message_review_items WHERE review_status='open') AS review_open,
                        (SELECT COUNT(*)::int FROM message_review_items WHERE review_status='open' AND created_at>=now()-interval '24 hours') AS review_open_24h,
                        (SELECT COUNT(*)::int FROM messages WHERE ingestion_status='error' AND created_at>=now()-interval '24 hours') AS message_errors_24h,
                        (SELECT COUNT(*)::int FROM telegram_publications WHERE status='failed') AS publication_failed,
                        (SELECT COUNT(*)::int FROM telegram_publications WHERE status IN ('pending','sending')) AS publication_pending,
                        (SELECT COUNT(*)::int FROM push_notification_deliveries WHERE status='failed' AND COALESCE(attempted_at,created_at)>=now()-interval '24 hours') AS push_failed_24h,
                        (SELECT COUNT(*)::int FROM telegram_notification_deliveries WHERE status='failed' AND COALESCE(attempted_at,created_at)>=now()-interval '24 hours') AS telegram_notification_failed_24h,
                        EXISTS(
                            SELECT 1 FROM telegram_live_board_state
                            WHERE id=1 AND status='ready' AND telegram_message_id IS NOT NULL
                        ) AS live_board_ready,
                        EXISTS(
                            SELECT 1 FROM telegram_live_board_state
                            WHERE id=1 AND pinned_at IS NOT NULL
                        ) AS live_board_pinned
                    """
                )
            ).mappings().one()

            stage_rows = session.execute(
                text(
                    """
                    SELECT review_stage,
                           COUNT(*)::int AS open_count,
                           MIN(created_at) AS oldest_at,
                           MAX(created_at) AS newest_at
                    FROM message_review_items
                    WHERE review_status='open'
                    GROUP BY review_stage
                    ORDER BY open_count DESC, review_stage
                    """
                )
            ).mappings().all()

            event_rows = session.execute(
                text(
                    """
                    SELECT event_type, entity_type, created_at
                    FROM audit_events
                    WHERE event_type LIKE 'trading.%'
                       OR event_type LIKE 'admin.%'
                       OR event_type LIKE 'source.%'
                       OR event_type LIKE 'mt5.day26_execution_%'
                       OR event_type LIKE 'mt5.day27_%'
                       OR event_type LIKE 'mt5.day30_%'
                    ORDER BY created_at DESC, id DESC
                    LIMIT 10
                    """
                )
            ).mappings().all()

        metrics = {key: self._int(value) for key, value in counts.items() if key not in {"live_board_ready", "live_board_pinned"}}
        attention = self._attention(metrics, bool(counts["live_board_ready"]), bool(counts["live_board_pinned"]))
        overall = self._overall_status(attention)
        return Day35ControlCentreView(
            generated_at=now,
            overall_status=overall,
            open_signals=metrics["open_signals"],
            pending_signals=metrics["pending_signals"],
            trading_active_users=metrics["trading_active_users"],
            trading_stopped_users=metrics["trading_stopped_users"],
            source_live=metrics["source_live"],
            source_testing=metrics["source_testing"],
            source_paused=metrics["source_paused"],
            source_revoked=metrics["source_revoked"],
            telegram_connected=metrics["telegram_connected"],
            telegram_attention=metrics["telegram_attention"],
            mt5_connected=metrics["mt5_connected"],
            mt5_attention=metrics["mt5_attention"],
            active_users=metrics["active_users"],
            invited_users=metrics["invited_users"],
            suspended_users=metrics["suspended_users"],
            revoked_users=metrics["revoked_users"],
            review_open=metrics["review_open"],
            review_open_24h=metrics["review_open_24h"],
            message_errors_24h=metrics["message_errors_24h"],
            publication_failed=metrics["publication_failed"],
            publication_pending=metrics["publication_pending"],
            push_failed_24h=metrics["push_failed_24h"],
            telegram_notification_failed_24h=metrics["telegram_notification_failed_24h"],
            live_board_ready=bool(counts["live_board_ready"]),
            live_board_pinned=bool(counts["live_board_pinned"]),
            review_stages=tuple(
                Day35ReviewStage(
                    stage=str(row["review_stage"]),
                    open_count=int(row["open_count"]),
                    oldest_at=row["oldest_at"],
                    newest_at=row["newest_at"],
                )
                for row in stage_rows
            ),
            attention=attention,
            recent_events=tuple(
                Day35RecentEvent(
                    event_type=str(row["event_type"]),
                    entity_type=str(row["entity_type"]),
                    created_at=row["created_at"],
                )
                for row in event_rows
            ),
        )

    @staticmethod
    def _attention(
        metrics: dict[str, int],
        live_board_ready: bool,
        live_board_pinned: bool,
    ) -> tuple[Day35AttentionItem, ...]:
        items: list[Day35AttentionItem] = []
        if metrics["mt5_attention"]:
            items.append(Day35AttentionItem(
                key="mt5",
                tone="critical",
                title="MT5 connection needs attention",
                detail="One or more non-revoked MT5 accounts are not connected.",
                count=metrics["mt5_attention"],
                area="mt5",
            ))
        delivery_failures = metrics["publication_failed"] + metrics["push_failed_24h"] + metrics["telegram_notification_failed_24h"]
        if delivery_failures:
            items.append(Day35AttentionItem(
                key="delivery",
                tone="critical",
                title="Notification delivery failures",
                detail="Trading is unaffected, but one or more member/admin deliveries need review.",
                count=delivery_failures,
                area="activity",
            ))
        if metrics["message_errors_24h"]:
            items.append(Day35AttentionItem(
                key="ingestion",
                tone="critical",
                title="Telegram ingestion errors",
                detail="Source messages entered an error state during the last 24 hours.",
                count=metrics["message_errors_24h"],
                area="sources",
            ))
        if not live_board_ready or not live_board_pinned:
            items.append(Day35AttentionItem(
                key="live-board",
                tone="attention",
                title="Live Trades Board is not fully ready",
                detail="The shared Telegram board should have one ready, pinned bot-authored message.",
                count=1,
                area="activity",
            ))
        if metrics["review_open"]:
            items.append(Day35AttentionItem(
                key="review",
                tone="attention",
                title="Signal review queue has open items",
                detail="Classification, parsing or validation items are waiting in the admin review queue.",
                count=metrics["review_open"],
                area="reviews",
            ))
        if metrics["source_paused"]:
            items.append(Day35AttentionItem(
                key="paused-sources",
                tone="neutral",
                title="Signal sources are paused",
                detail="Paused is an intentional safe state, but the operator should know those sources are not flowing live.",
                count=metrics["source_paused"],
                area="sources",
            ))
        if not items:
            items.append(Day35AttentionItem(
                key="healthy",
                tone="healthy",
                title="No operational attention required",
                detail="Core ingestion, broker and delivery checks are clear at this snapshot.",
                count=0,
                area="overview",
            ))
        return tuple(items)

    @staticmethod
    def _overall_status(items: tuple[Day35AttentionItem, ...]) -> ControlTone:
        if any(item.tone == "critical" for item in items):
            return "critical"
        if any(item.tone == "attention" for item in items):
            return "attention"
        return "healthy"

    @staticmethod
    def _int(value: Any) -> int:
        return int(value or 0)


__all__ = [
    "Day35AttentionItem",
    "Day35ControlCentreService",
    "Day35ControlCentreView",
    "Day35RecentEvent",
    "Day35ReviewStage",
]
