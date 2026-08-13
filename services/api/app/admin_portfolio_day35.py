"""Day 35 admin Signal Portfolio read model.

This layer deliberately reuses Day 33's broker-backed outcome rows and canonical
_summary_metrics implementation. It never derives performance from Telegram claims and
never aggregates multiple user copies of the same signal. The Owner account is the
single reference execution ledger for provider/trader comparison.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import text

from app.performance_ledger_day33 import _d, _pct, stable_color_index
from app.performance_ledger_day33_v2 import Day33PerformanceLedgerServiceV2

PeriodKey = Literal["today", "7d", "month", "year", "all"]
SortKey = Literal["realized_pnl", "return_percent", "win_rate", "trade_count"]


@dataclass(frozen=True, slots=True)
class Day35PortfolioRow:
    dimension_type: str
    source_id: UUID
    source_label: str
    trader_stream: str | None
    source_color_index: int
    realized_cash_pnl: Decimal
    open_cash_pnl: Decimal | None
    open_cash_pnl_known: bool
    return_percent: Decimal | None
    trades_closed: int
    trades_open: int
    wins: int
    losses: int
    breakeven: int
    win_rate_percent: Decimal | None
    net_pips: Decimal | None
    rank: int = 0


@dataclass(frozen=True, slots=True)
class Day35PortfolioView:
    period_key: PeriodKey
    period_label: str
    period_start: datetime
    period_end: datetime
    reference_user_id: UUID
    rows: tuple[Day35PortfolioRow, ...]
    performance_basis: str = "day33_broker_deal_ledger"
    provider_identity_visible: bool = True
    broker_trade_action_created: bool = False


class Day35AdminPortfolioService:
    """Read-only source/trader comparison over the canonical Day 33 ledger."""

    def __init__(self, ledger: Day33PerformanceLedgerServiceV2) -> None:
        self._ledger = ledger
        self._session_factory = ledger._session_factory

    def read(
        self,
        period: PeriodKey,
        *,
        sort_by: SortKey = "realized_pnl",
        now: datetime | None = None,
    ) -> Day35PortfolioView:
        point = now or datetime.now(UTC)
        if point.tzinfo is None:
            point = point.replace(tzinfo=UTC)
        else:
            point = point.astimezone(UTC)
        start, label = self._period_start(period, point)
        reference_user_id = self._owner_reference_user_id()
        outcome_rows = self._outcomes(reference_user_id, start=start, end=point)

        dimensions: dict[tuple[str, UUID, str | None], list[Any]] = {}
        labels: dict[UUID, str] = {}
        for row in outcome_rows:
            source_id = row["source_id"]
            if not isinstance(source_id, UUID):
                continue
            source_label = str(row["source_label"] or "Unknown source")
            labels[source_id] = source_label
            dimensions.setdefault(("source", source_id, None), []).append(row)
            trader_stream = str(row["trader_stream"] or "").strip() or None
            if trader_stream is not None:
                dimensions.setdefault(("trader", source_id, trader_stream), []).append(row)

        result: list[Day35PortfolioRow] = []
        for (dimension_type, source_id, trader_stream), rows in dimensions.items():
            metrics = self._ledger._summary_metrics(
                reference_user_id,
                rows,
                start,
                point,
            )
            wins = int(metrics["wins"])
            losses = int(metrics["losses"])
            decided = wins + losses
            win_rate = (
                _pct(Decimal(wins) / Decimal(decided) * Decimal("100"))
                if decided
                else None
            )
            result.append(
                Day35PortfolioRow(
                    dimension_type=dimension_type,
                    source_id=source_id,
                    source_label=labels[source_id],
                    trader_stream=trader_stream,
                    source_color_index=stable_color_index(source_id, trader_stream),
                    realized_cash_pnl=_d(metrics["cash_pnl"]),
                    # Floating P/L must come from a live broker read. Until that Day 35
                    # slice is wired, unknown stays None rather than becoming false zero.
                    open_cash_pnl=None,
                    open_cash_pnl_known=False,
                    return_percent=(
                        _d(metrics["return_percent"])
                        if metrics["return_percent"] is not None
                        else None
                    ),
                    trades_closed=int(metrics["total_trades"]),
                    trades_open=int(metrics["open_trades"]),
                    wins=wins,
                    losses=losses,
                    breakeven=int(metrics["breakeven"]),
                    win_rate_percent=win_rate,
                    net_pips=(
                        _d(metrics["net_pips"])
                        if metrics["net_pips"] is not None
                        else None
                    ),
                )
            )

        ordered = sorted(result, key=lambda row: self._sort_value(row, sort_by), reverse=True)
        ranked = tuple(replace(row, rank=index) for index, row in enumerate(ordered, start=1))
        return Day35PortfolioView(
            period_key=period,
            period_label=label,
            period_start=start,
            period_end=point,
            reference_user_id=reference_user_id,
            rows=ranked,
        )

    @staticmethod
    def _period_start(period: PeriodKey, now: datetime) -> tuple[datetime, str]:
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if period == "today":
            return today, "Today"
        if period == "7d":
            return now - timedelta(days=7), "7 days"
        if period == "month":
            return today.replace(day=1), "Month"
        if period == "year":
            return today.replace(month=1, day=1), "Year"
        if period == "all":
            return datetime(1970, 1, 1, tzinfo=UTC), "All time"
        raise ValueError("Unsupported Day 35 portfolio period")

    @staticmethod
    def _sort_value(row: Day35PortfolioRow, sort_by: SortKey) -> tuple[Decimal, Decimal]:
        if sort_by == "return_percent":
            return (row.return_percent or Decimal("-999999999"), row.realized_cash_pnl)
        if sort_by == "win_rate":
            return (row.win_rate_percent or Decimal("-1"), row.realized_cash_pnl)
        if sort_by == "trade_count":
            return (Decimal(row.trades_closed + row.trades_open), row.realized_cash_pnl)
        return (row.realized_cash_pnl, row.return_percent or Decimal("-999999999"))

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

    def _outcomes(
        self,
        user_id: UUID,
        *,
        start: datetime,
        end: datetime,
    ) -> list[Any]:
        with self._session_factory() as session:
            return list(
                session.execute(
                    text(
                        """
                        SELECT
                            o.*,
                            COALESCE(src.source_alias,src.chat_title,'Unknown source') AS source_label
                        FROM performance_trade_outcomes o
                        LEFT JOIN sources src ON src.id=o.source_id
                        WHERE o.user_id=:user_id
                          AND (
                              (o.closed_at IS NOT NULL AND o.closed_at>=:period_start AND o.closed_at<=:period_end)
                              OR (
                                  o.status IN ('open','pending')
                                  AND o.opened_at IS NOT NULL
                                  AND o.opened_at>=:period_start
                                  AND o.opened_at<=:period_end
                              )
                          )
                        ORDER BY COALESCE(o.closed_at,o.opened_at),o.position_id
                        """
                    ),
                    {
                        "user_id": user_id,
                        "period_start": start,
                        "period_end": end,
                    },
                ).mappings().all()
            )


__all__ = [
    "Day35AdminPortfolioService",
    "Day35PortfolioRow",
    "Day35PortfolioView",
    "PeriodKey",
    "SortKey",
]
