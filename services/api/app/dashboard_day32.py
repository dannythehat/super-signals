"""Day 32 mobile dashboard read model.

The dashboard is observability only. Broker state is authoritative for live account
values and open bot positions. A dashboard read may reconcile stale local position
flags when the mapped broker position no longer exists, but it never sends a trade,
close, modify, or pending-order request to MetaAPI.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.models import AuditEvent
from app.mt5_read_service_day23 import Day23Mt5ReadService, Day23ReadError, Day23LiveState


@dataclass(frozen=True, slots=True)
class Day32Connection:
    configured: bool
    status: str
    account_environment: str | None
    login_masked: str | None
    server: str | None
    error_code: str | None
    read_at: datetime | None


@dataclass(frozen=True, slots=True)
class Day32Account:
    currency: str
    balance: float
    equity: float
    margin: float
    free_margin: float
    trade_allowed: bool


@dataclass(frozen=True, slots=True)
class Day32Trading:
    available: bool
    status: str | None
    risk_percent: Decimal | None
    allow_double_lot: bool | None
    effective_double_lot_risk_percent: Decimal | None


@dataclass(frozen=True, slots=True)
class Day32PerformancePeriod:
    key: str
    label: str
    amount: Decimal | None
    known_position_count: int
    provisional_until_day33: bool = True


@dataclass(frozen=True, slots=True)
class Day32OpenPosition:
    position_id: UUID
    broker_position_id: str
    signal_id: UUID
    tp_index: int
    symbol: str
    side: str
    volume: float
    planned_risk_percent: Decimal
    entry_price: float
    current_price: float | None
    stop_loss: float | None
    take_profit: float | None
    profit: float | None
    opened_at: datetime | None


@dataclass(frozen=True, slots=True)
class Day32LatestSignal:
    signal_id: UUID
    symbol: str
    side: str
    created_at: datetime
    position_count: int
    open_positions: int
    closed_positions: int
    status: str


@dataclass(frozen=True, slots=True)
class Day32CompletedPosition:
    position_id: UUID
    signal_id: UUID
    tp_index: int
    symbol: str
    side: str
    closed_at: datetime | None
    pnl_amount: Decimal | None
    close_reason: str | None


@dataclass(frozen=True, slots=True)
class Day32WinLoss:
    wins: int
    losses: int
    breakeven: int
    known_results: int
    win_rate_percent: Decimal | None


@dataclass(frozen=True, slots=True)
class Day32Activity:
    event_type: str
    label: str
    tone: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Day32DashboardView:
    connection: Day32Connection
    account: Day32Account | None
    trading: Day32Trading
    open_profit: float | None
    open_positions: tuple[Day32OpenPosition, ...]
    latest_signal: Day32LatestSignal | None
    recent_completed: tuple[Day32CompletedPosition, ...]
    performance: tuple[Day32PerformancePeriod, ...]
    win_loss: Day32WinLoss
    activity: tuple[Day32Activity, ...]
    reconciled_external_positions: int


class QuietDay23Mt5ReadService(Day23Mt5ReadService):
    """Reuse the proven Day 23 terminal reader without creating poll-noise audits."""

    def _audit_success(self, state: Day23LiveState) -> None:  # noqa: ARG002
        return

    def _audit_failure(  # noqa: ARG002
        self,
        local_account_id: UUID,
        code: str,
        stage: str,
        *,
        region: str | None = None,
    ) -> None:
        return


class Day32DashboardService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        read_service: Day23Mt5ReadService,
    ) -> None:
        self._session_factory = session_factory
        self._read_service = read_service

    async def read(self, user_id: UUID) -> Day32DashboardView:
        account_row = self._account_row(user_id)
        trading = self._trading(user_id)
        now = datetime.now(UTC)

        if account_row is None:
            return self._without_live_state(
                connection=Day32Connection(
                    configured=False,
                    status="not_configured",
                    account_environment=None,
                    login_masked=None,
                    server=None,
                    error_code=None,
                    read_at=None,
                ),
                trading=trading,
                user_id=user_id,
                now=now,
            )

        connection = Day32Connection(
            configured=True,
            status=str(account_row["status"]),
            account_environment=str(account_row["account_environment"]),
            login_masked=self._mask_login(str(account_row["login"])),
            server=str(account_row["server"]),
            error_code=(str(account_row["last_error_code"]) if account_row["last_error_code"] else None),
            read_at=None,
        )
        if str(account_row["status"]) != "connected":
            return self._without_live_state(
                connection=connection,
                trading=trading,
                user_id=user_id,
                now=now,
            )

        try:
            live = await self._read_service.read_owner_live_state(user_id)
        except Day23ReadError as exc:
            return self._without_live_state(
                connection=Day32Connection(
                    configured=True,
                    status="connection_error",
                    account_environment=str(account_row["account_environment"]),
                    login_masked=self._mask_login(str(account_row["login"])),
                    server=str(account_row["server"]),
                    error_code=exc.code,
                    read_at=now,
                ),
                trading=trading,
                user_id=user_id,
                now=now,
            )

        broker_positions = {item.position_id: item for item in live.positions if item.position_id}
        reconciled = self._reconcile_missing_open_positions(
            user_id=user_id,
            broker_position_ids=set(broker_positions),
            now=now,
        )
        open_positions = self._mapped_open_positions(user_id, broker_positions)
        open_profit_values = [item.profit for item in open_positions if item.profit is not None]
        open_profit = sum(open_profit_values) if open_profit_values else (0.0 if open_positions else 0.0)

        return Day32DashboardView(
            connection=Day32Connection(
                configured=True,
                status="connected",
                account_environment=str(account_row["account_environment"]),
                login_masked=live.login_masked,
                server=live.server,
                error_code=None,
                read_at=live.read_at,
            ),
            account=Day32Account(
                currency=live.account.currency,
                balance=live.account.balance,
                equity=live.account.equity,
                margin=live.account.margin,
                free_margin=live.account.free_margin,
                trade_allowed=live.account.trade_allowed,
            ),
            trading=trading,
            open_profit=open_profit,
            open_positions=open_positions,
            latest_signal=self._latest_signal(user_id),
            recent_completed=self._recent_completed(user_id),
            performance=self._performance(user_id, now),
            win_loss=self._win_loss(user_id),
            activity=self._activity(user_id),
            reconciled_external_positions=reconciled,
        )

    def _without_live_state(
        self,
        *,
        connection: Day32Connection,
        trading: Day32Trading,
        user_id: UUID,
        now: datetime,
    ) -> Day32DashboardView:
        return Day32DashboardView(
            connection=connection,
            account=None,
            trading=trading,
            open_profit=None,
            open_positions=(),
            latest_signal=self._latest_signal(user_id),
            recent_completed=self._recent_completed(user_id),
            performance=self._performance(user_id, now),
            win_loss=self._win_loss(user_id),
            activity=self._activity(user_id),
            reconciled_external_positions=0,
        )

    def _account_row(self, user_id: UUID) -> Any | None:
        with self._session_factory() as session:
            return session.execute(
                text(
                    """
                    SELECT id, login, server, status, account_environment, last_error_code
                    FROM mt5_accounts
                    WHERE owner_user_id=:user_id AND status!='revoked'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {"user_id": user_id},
            ).mappings().first()

    def _trading(self, user_id: UUID) -> Day32Trading:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT risk_percent, allow_double_lot, trading_status
                    FROM user_trading_controls
                    WHERE user_id=:user_id
                    LIMIT 1
                    """
                ),
                {"user_id": user_id},
            ).mappings().first()
        if row is None:
            return Day32Trading(
                available=False,
                status=None,
                risk_percent=None,
                allow_double_lot=None,
                effective_double_lot_risk_percent=None,
            )
        risk = Decimal(str(row["risk_percent"]))
        allow_double = bool(row["allow_double_lot"])
        return Day32Trading(
            available=True,
            status=str(row["trading_status"]),
            risk_percent=risk,
            allow_double_lot=allow_double,
            effective_double_lot_risk_percent=(risk * Decimal("2") if allow_double else risk),
        )

    def _reconcile_missing_open_positions(
        self,
        *,
        user_id: UUID,
        broker_position_ids: set[str],
        now: datetime,
    ) -> int:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT id, broker_position_id
                    FROM positions
                    WHERE user_id=:user_id
                      AND status='open'
                      AND broker_position_id IS NOT NULL
                    ORDER BY created_at, id
                    """
                ),
                {"user_id": user_id},
            ).mappings().all()
            missing = [row for row in rows if str(row["broker_position_id"]) not in broker_position_ids]
            if not missing:
                return 0
            for row in missing:
                session.execute(
                    text(
                        """
                        UPDATE positions
                        SET status='closed',
                            closed_at=COALESCE(closed_at,:now),
                            close_reason=COALESCE(close_reason,'external_close'),
                            updated_at=:now
                        WHERE id=:id AND status='open'
                        """
                    ),
                    {"id": row["id"], "now": now},
                )
                session.add(
                    AuditEvent(
                        actor_user_id=user_id,
                        event_type="dashboard.position_reconciled",
                        entity_type="position",
                        entity_id=row["id"],
                        payload={
                            "reason": "broker_position_absent",
                            "broker_position_id": str(row["broker_position_id"]),
                            "local_status_before": "open",
                            "local_status_after": "closed",
                            "close_reason": "external_close",
                            "trade_action_created": False,
                        },
                    )
                )
            session.commit()
            return len(missing)

    def _mapped_open_positions(
        self,
        user_id: UUID,
        broker_positions: dict[str, Any],
    ) -> tuple[Day32OpenPosition, ...]:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT p.id, p.signal_id, p.tp_index, p.planned_risk_percent,
                           p.broker_position_id, s.symbol, s.side
                    FROM positions p
                    JOIN signals s ON s.id=p.signal_id
                    WHERE p.user_id=:user_id
                      AND p.status='open'
                      AND p.broker_position_id IS NOT NULL
                    ORDER BY p.opened_at NULLS LAST, p.tp_index, p.id
                    """
                ),
                {"user_id": user_id},
            ).mappings().all()
        result: list[Day32OpenPosition] = []
        for row in rows:
            broker_id = str(row["broker_position_id"])
            broker = broker_positions.get(broker_id)
            if broker is None:
                continue
            result.append(
                Day32OpenPosition(
                    position_id=row["id"],
                    broker_position_id=broker_id,
                    signal_id=row["signal_id"],
                    tp_index=int(row["tp_index"]),
                    symbol=str(broker.symbol or row["symbol"] or ""),
                    side=str(broker.side or row["side"] or ""),
                    volume=float(broker.volume),
                    planned_risk_percent=Decimal(str(row["planned_risk_percent"])),
                    entry_price=float(broker.open_price),
                    current_price=broker.current_price,
                    stop_loss=broker.stop_loss,
                    take_profit=broker.take_profit,
                    profit=broker.profit,
                    opened_at=broker.opened_at,
                )
            )
        return tuple(result)

    def _latest_signal(self, user_id: UUID) -> Day32LatestSignal | None:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT s.id AS signal_id, COALESCE(s.symbol,'') AS symbol,
                           COALESCE(s.side,'') AS side, s.created_at,
                           COUNT(p.id)::int AS position_count,
                           COUNT(p.id) FILTER (WHERE p.status='open')::int AS open_positions,
                           COUNT(p.id) FILTER (WHERE p.status='closed')::int AS closed_positions
                    FROM positions p
                    JOIN signals s ON s.id=p.signal_id
                    WHERE p.user_id=:user_id
                    GROUP BY s.id, s.symbol, s.side, s.created_at
                    ORDER BY MAX(p.created_at) DESC
                    LIMIT 1
                    """
                ),
                {"user_id": user_id},
            ).mappings().first()
        if row is None:
            return None
        total = int(row["position_count"])
        opened = int(row["open_positions"])
        closed = int(row["closed_positions"])
        status = "active" if opened else "closed" if total and closed == total else "processing"
        return Day32LatestSignal(
            signal_id=row["signal_id"],
            symbol=str(row["symbol"]),
            side=str(row["side"]),
            created_at=row["created_at"],
            position_count=total,
            open_positions=opened,
            closed_positions=closed,
            status=status,
        )

    def _recent_completed(self, user_id: UUID) -> tuple[Day32CompletedPosition, ...]:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT p.id, p.signal_id, p.tp_index, p.closed_at, p.pnl_amount,
                           p.close_reason, COALESCE(s.symbol,'') AS symbol,
                           COALESCE(s.side,'') AS side
                    FROM positions p
                    JOIN signals s ON s.id=p.signal_id
                    WHERE p.user_id=:user_id AND p.status='closed'
                    ORDER BY COALESCE(p.closed_at,p.updated_at) DESC
                    LIMIT 6
                    """
                ),
                {"user_id": user_id},
            ).mappings().all()
        return tuple(
            Day32CompletedPosition(
                position_id=row["id"],
                signal_id=row["signal_id"],
                tp_index=int(row["tp_index"]),
                symbol=str(row["symbol"]),
                side=str(row["side"]),
                closed_at=row["closed_at"],
                pnl_amount=(Decimal(str(row["pnl_amount"])) if row["pnl_amount"] is not None else None),
                close_reason=(str(row["close_reason"]) if row["close_reason"] else None),
            )
            for row in rows
        )

    def _performance(
        self,
        user_id: UUID,
        now: datetime,
    ) -> tuple[Day32PerformancePeriod, ...]:
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        periods = (
            ("today", "Today", today),
            ("7d", "7 days", now - timedelta(days=7)),
            ("30d", "30 days", now - timedelta(days=30)),
        )
        result: list[Day32PerformancePeriod] = []
        with self._session_factory() as session:
            for key, label, since in periods:
                row = session.execute(
                    text(
                        """
                        SELECT COUNT(*)::int AS known_count,
                               SUM(pnl_amount) AS amount
                        FROM positions
                        WHERE user_id=:user_id
                          AND status='closed'
                          AND pnl_amount IS NOT NULL
                          AND closed_at IS NOT NULL
                          AND closed_at >= :since
                        """
                    ),
                    {"user_id": user_id, "since": since},
                ).mappings().one()
                count = int(row["known_count"])
                result.append(
                    Day32PerformancePeriod(
                        key=key,
                        label=label,
                        amount=(Decimal(str(row["amount"])) if count and row["amount"] is not None else None),
                        known_position_count=count,
                    )
                )
        return tuple(result)

    def _win_loss(self, user_id: UUID) -> Day32WinLoss:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT COUNT(*) FILTER (WHERE pnl_amount > 0)::int AS wins,
                           COUNT(*) FILTER (WHERE pnl_amount < 0)::int AS losses,
                           COUNT(*) FILTER (WHERE pnl_amount = 0)::int AS breakeven
                    FROM positions
                    WHERE user_id=:user_id AND status='closed' AND pnl_amount IS NOT NULL
                    """
                ),
                {"user_id": user_id},
            ).mappings().one()
        wins = int(row["wins"])
        losses = int(row["losses"])
        breakeven = int(row["breakeven"])
        known = wins + losses + breakeven
        decided = wins + losses
        rate = (
            (Decimal(wins) / Decimal(decided) * Decimal("100")).quantize(Decimal("0.1"))
            if decided
            else None
        )
        return Day32WinLoss(
            wins=wins,
            losses=losses,
            breakeven=breakeven,
            known_results=known,
            win_rate_percent=rate,
        )

    def _activity(self, user_id: UUID) -> tuple[Day32Activity, ...]:
        labels = {
            "trading.automation_activated": ("Automated trading activated", "positive"),
            "trading.stop_requested": ("Automated trading stopped", "neutral"),
            "trading.stop_close_completed": ("Stop & Close completed", "neutral"),
            "trading.risk_settings_changed": ("Risk settings updated", "neutral"),
            "dashboard.position_reconciled": ("Broker position reconciled", "neutral"),
        }
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT event_type, created_at
                    FROM audit_events
                    WHERE actor_user_id=:user_id
                      AND event_type IN (
                        'trading.automation_activated',
                        'trading.stop_requested',
                        'trading.stop_close_completed',
                        'trading.risk_settings_changed',
                        'dashboard.position_reconciled'
                      )
                    ORDER BY created_at DESC, id DESC
                    LIMIT 8
                    """
                ),
                {"user_id": user_id},
            ).mappings().all()
        return tuple(
            Day32Activity(
                event_type=str(row["event_type"]),
                label=labels[str(row["event_type"])][0],
                tone=labels[str(row["event_type"])][1],
                created_at=row["created_at"],
            )
            for row in rows
        )

    @staticmethod
    def _mask_login(login: str) -> str:
        if len(login) <= 4:
            return "*" * len(login)
        return f"{'*' * max(4, len(login) - 4)}{login[-4:]}"
