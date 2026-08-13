"""Day 35 administrator trade/failure drill-down.

This module is read-only. Trade state comes from the Day 33 canonical performance
ledger using the single Owner reference execution ledger. Failure rows come only from
current persisted operational states or safe audit metadata; no raw credentials or
private broker payloads are exposed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.performance_ledger_day33_v2 import Day33PerformanceLedgerServiceV2
from app.trade_identity import public_trade_identity


@dataclass(frozen=True, slots=True)
class Day35AdminTrade:
    signal_id: UUID
    public_reference: str
    public_marker: str
    symbol: str
    side: str
    status: str
    status_label: str
    status_color: str
    source_label: str | None
    trader_stream: str | None
    source_color_index: int | None
    opened_at: datetime | None
    closed_at: datetime | None
    position_count: int
    open_positions: int
    pending_positions: int
    closed_positions: int
    cash_pnl: Decimal | None
    net_pips: Decimal | None
    model_500_pnl: Decimal | None
    close_reason: str | None
    telegram_root_published: bool


@dataclass(frozen=True, slots=True)
class Day35CurrentFailure:
    failure_type: str
    severity: str
    title: str
    detail: str
    failure_code: str | None
    occurred_at: datetime
    signal_id: UUID | None
    public_reference: str | None
    source_label: str | None


@dataclass(frozen=True, slots=True)
class Day35FailureHistory:
    event_type: str
    entity_type: str
    error_code: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Day35OperationsView:
    generated_at: datetime
    trades: tuple[Day35AdminTrade, ...]
    current_failures: tuple[Day35CurrentFailure, ...]
    recent_failure_history: tuple[Day35FailureHistory, ...]
    current_failure_count: int
    provider_identity_visible: bool = True
    broker_trade_action_created: bool = False


class Day35OperationsService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        ledger: Day33PerformanceLedgerServiceV2,
    ) -> None:
        self._session_factory = session_factory
        self._ledger = ledger

    def read(self, *, trade_limit: int = 200, failure_limit: int = 100) -> Day35OperationsView:
        reference_user_id = self._owner_reference_user_id()
        timeline = self._ledger.read_timeline(
            reference_user_id,
            viewer_role="owner",
            limit=max(1, min(trade_limit, 250)),
        )
        published_roots = self._published_roots(tuple(item.signal_id for item in timeline.trades))
        trades: list[Day35AdminTrade] = []
        for item in timeline.trades:
            identity = public_trade_identity(item.signal_id)
            trades.append(
                Day35AdminTrade(
                    signal_id=item.signal_id,
                    public_reference=identity.reference,
                    public_marker=identity.marker,
                    symbol=item.symbol,
                    side=item.side,
                    status=item.status,
                    status_label=item.status_label,
                    status_color=item.status_color,
                    source_label=item.source_label,
                    trader_stream=item.trader_stream,
                    source_color_index=item.source_color_index,
                    opened_at=item.opened_at,
                    closed_at=item.closed_at,
                    position_count=item.position_count,
                    open_positions=item.open_positions,
                    pending_positions=item.pending_positions,
                    closed_positions=item.closed_positions,
                    cash_pnl=item.cash_pnl,
                    net_pips=item.net_pips,
                    model_500_pnl=item.model_500_pnl,
                    close_reason=item.close_reason,
                    telegram_root_published=item.signal_id in published_roots,
                )
            )

        failures = self._current_failures(limit=max(1, min(failure_limit, 200)))
        history = self._failure_history(limit=50)
        return Day35OperationsView(
            generated_at=datetime.now(UTC),
            trades=tuple(trades),
            current_failures=tuple(failures),
            recent_failure_history=tuple(history),
            current_failure_count=len(failures),
        )

    def _owner_reference_user_id(self) -> UUID:
        with self._session_factory() as session:
            value = session.execute(
                text(
                    """
                    SELECT u.id
                    FROM users u
                    JOIN user_roles ur ON ur.user_id=u.id
                    JOIN roles r ON r.id=ur.role_id
                    WHERE r.name='owner'
                      AND u.status NOT IN ('revoked','suspended')
                    ORDER BY u.created_at,u.id
                    LIMIT 1
                    """
                )
            ).scalar_one_or_none()
        if not isinstance(value, UUID):
            raise RuntimeError("day35_owner_reference_user_missing")
        return value

    def _published_roots(self, signal_ids: tuple[UUID, ...]) -> set[UUID]:
        if not signal_ids:
            return set()
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT DISTINCT pub.signal_id
                    FROM telegram_publications pub
                    WHERE pub.publication_kind='signal_created'
                      AND pub.lifecycle_event_id IS NULL
                      AND pub.status='sent'
                      AND pub.telegram_message_id IS NOT NULL
                      AND pub.signal_id IN (
                          SELECT s.id FROM signals s
                          WHERE s.id::text = ANY(string_to_array(:signal_ids, ','))
                      )
                    """
                ),
                {"signal_ids": ",".join(str(value) for value in signal_ids)},
            ).scalars().all()
        return {value for value in rows if isinstance(value, UUID)}

    def _current_failures(self, *, limit: int) -> list[Day35CurrentFailure]:
        rows: list[Day35CurrentFailure] = []
        with self._session_factory() as session:
            message_rows = session.execute(
                text(
                    """
                    SELECT m.created_at,
                           COALESCE(src.source_alias,src.chat_title,'Unknown source') AS source_label,
                           LEFT(COALESCE(m.raw_text,''),180) AS raw_text
                    FROM messages m
                    LEFT JOIN sources src ON src.id=m.source_id
                    WHERE m.ingestion_status='error'
                    ORDER BY m.created_at DESC
                    LIMIT :limit
                    """
                ),
                {"limit": limit},
            ).mappings().all()
            for row in message_rows:
                rows.append(
                    Day35CurrentFailure(
                        failure_type="message_ingestion",
                        severity="critical",
                        title="Telegram message ingestion error",
                        detail=str(row["raw_text"] or "Message could not complete ingestion."),
                        failure_code="ingestion_error",
                        occurred_at=row["created_at"],
                        signal_id=None,
                        public_reference=None,
                        source_label=str(row["source_label"]),
                    )
                )

            publication_rows = session.execute(
                text(
                    """
                    SELECT pub.signal_id,pub.failure_code,pub.failure_reason,
                           COALESCE(pub.attempted_at,pub.updated_at,pub.created_at) AS occurred_at,
                           COALESCE(src.source_alias,src.chat_title,'Unknown source') AS source_label
                    FROM telegram_publications pub
                    LEFT JOIN signals sig ON sig.id=pub.signal_id
                    LEFT JOIN sources src ON src.id=sig.source_id
                    WHERE pub.status='failed'
                    ORDER BY occurred_at DESC
                    LIMIT :limit
                    """
                ),
                {"limit": limit},
            ).mappings().all()
            for row in publication_rows:
                signal_id = row["signal_id"] if isinstance(row["signal_id"], UUID) else None
                rows.append(
                    Day35CurrentFailure(
                        failure_type="telegram_publication",
                        severity="critical",
                        title="Trade publication failed",
                        detail=str(row["failure_reason"] or "Telegram publication failed."),
                        failure_code=(str(row["failure_code"]) if row["failure_code"] else None),
                        occurred_at=row["occurred_at"],
                        signal_id=signal_id,
                        public_reference=(public_trade_identity(signal_id).reference if signal_id else None),
                        source_label=(str(row["source_label"]) if row["source_label"] else None),
                    )
                )

            push_rows = session.execute(
                text(
                    """
                    SELECT failure_code,failure_reason,COALESCE(attempted_at,updated_at,created_at) AS occurred_at
                    FROM push_notification_deliveries
                    WHERE status='failed'
                    ORDER BY occurred_at DESC
                    LIMIT :limit
                    """
                ),
                {"limit": limit},
            ).mappings().all()
            for row in push_rows:
                rows.append(
                    Day35CurrentFailure(
                        failure_type="push_delivery",
                        severity="attention",
                        title="Web Push delivery failed",
                        detail=str(row["failure_reason"] or "Private push delivery failed."),
                        failure_code=(str(row["failure_code"]) if row["failure_code"] else None),
                        occurred_at=row["occurred_at"],
                        signal_id=None,
                        public_reference=None,
                        source_label=None,
                    )
                )

            telegram_rows = session.execute(
                text(
                    """
                    SELECT failure_code,failure_reason,COALESCE(attempted_at,updated_at,created_at) AS occurred_at
                    FROM telegram_notification_deliveries
                    WHERE status='failed'
                    ORDER BY occurred_at DESC
                    LIMIT :limit
                    """
                ),
                {"limit": limit},
            ).mappings().all()
            for row in telegram_rows:
                rows.append(
                    Day35CurrentFailure(
                        failure_type="telegram_notification",
                        severity="attention",
                        title="Telegram notification delivery failed",
                        detail=str(row["failure_reason"] or "Telegram notification delivery failed."),
                        failure_code=(str(row["failure_code"]) if row["failure_code"] else None),
                        occurred_at=row["occurred_at"],
                        signal_id=None,
                        public_reference=None,
                        source_label=None,
                    )
                )

            account_rows = session.execute(
                text(
                    """
                    SELECT a.status,a.last_error_code,a.updated_at,u.email::text AS email
                    FROM mt5_accounts a
                    JOIN users u ON u.id=a.owner_user_id
                    WHERE a.status NOT IN ('connected','revoked')
                    ORDER BY a.updated_at DESC
                    LIMIT :limit
                    """
                ),
                {"limit": limit},
            ).mappings().all()
            for row in account_rows:
                rows.append(
                    Day35CurrentFailure(
                        failure_type="mt5_connection",
                        severity="critical",
                        title="MT5 account needs attention",
                        detail=f"{row['email']} · status {row['status']}",
                        failure_code=(str(row["last_error_code"]) if row["last_error_code"] else None),
                        occurred_at=row["updated_at"],
                        signal_id=None,
                        public_reference=None,
                        source_label=None,
                    )
                )

            board = session.execute(
                text(
                    """
                    SELECT status,pinned_at,failure_code,failure_reason,updated_at
                    FROM telegram_live_board_state
                    WHERE id=1
                    """
                )
            ).mappings().first()
            if board is not None and (str(board["status"]) != "ready" or board["pinned_at"] is None):
                rows.append(
                    Day35CurrentFailure(
                        failure_type="live_board",
                        severity="attention",
                        title="Live Trades Board is not fully ready",
                        detail=str(board["failure_reason"] or f"Board status: {board['status']}"),
                        failure_code=(str(board["failure_code"]) if board["failure_code"] else None),
                        occurred_at=board["updated_at"],
                        signal_id=None,
                        public_reference=None,
                        source_label=None,
                    )
                )

        rows.sort(key=lambda item: item.occurred_at, reverse=True)
        return rows[:limit]

    def _failure_history(self, *, limit: int) -> list[Day35FailureHistory]:
        since = datetime.now(UTC) - timedelta(days=14)
        with self._session_factory() as session:
            values = session.execute(
                text(
                    """
                    SELECT event_type,entity_type,created_at,
                           COALESCE(payload->>'error_code',payload->>'failure_code',payload->>'code') AS error_code
                    FROM audit_events
                    WHERE created_at>=:since
                      AND (
                          event_type ILIKE '%fail%'
                          OR event_type ILIKE '%block%'
                          OR event_type ILIKE '%error%'
                          OR event_type ILIKE '%skip%'
                      )
                    ORDER BY created_at DESC,id DESC
                    LIMIT :limit
                    """
                ),
                {"since": since, "limit": limit},
            ).mappings().all()
        return [
            Day35FailureHistory(
                event_type=str(row["event_type"]),
                entity_type=str(row["entity_type"]),
                error_code=(str(row["error_code"]) if row["error_code"] else None),
                created_at=row["created_at"],
            )
            for row in values
        ]


__all__ = [
    "Day35AdminTrade",
    "Day35CurrentFailure",
    "Day35FailureHistory",
    "Day35OperationsService",
    "Day35OperationsView",
]
