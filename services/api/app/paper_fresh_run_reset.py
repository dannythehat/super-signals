"""Fresh paper-run visibility boundary for the Owner demo account.

The broker account, broker deals, audit events and historical outcomes remain intact as
forensic truth. The active Owner paper-test view is a separate run beginning at
``SUPER_SIGNALS_PAPER_RESET_AT``.

The reset contract is strict:
- no pre-reset signal may appear in Today, historical windows, timeline, recent trades,
  latest trade, activity, or visible open positions;
- the reset paper balance starts from ``SUPER_SIGNALS_PAPER_BASELINE_BALANCE`` (default
  1000) and changes only from broker-backed outcomes belonging to post-reset signals;
- pre-reset broker positions may continue to exist for audit/reconciliation purposes, but
  they are outside the active paper run and cannot contaminate its visible balance/P&L;
- 7d / 30d / Month / All time are explicit zeroes on the reset day and then accumulate
  only the new run on later days.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import text

from app.telegram_entity_recovery import install_telegram_entity_recovery

_installed = False
_ZERO = Decimal("0")
_DEFAULT_BASELINE = Decimal("1000")


def paper_reset_at() -> datetime | None:
    raw = os.getenv("SUPER_SIGNALS_PAPER_RESET_AT", "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def paper_owner_id() -> UUID | None:
    raw = os.getenv("SUPER_SIGNALS_DAY28_OWNER_ID", "").strip()
    try:
        return UUID(raw)
    except (ValueError, TypeError):
        return None


def paper_baseline_balance() -> Decimal:
    raw = os.getenv("SUPER_SIGNALS_PAPER_BASELINE_BALANCE", "1000").strip() or "1000"
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        return _DEFAULT_BASELINE
    return value if value > 0 else _DEFAULT_BASELINE


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _after(value: datetime | None, cutoff: datetime) -> bool:
    if value is None:
        return False
    return _utc(value) >= cutoff


def _same_reset_day(point: datetime, cutoff: datetime) -> bool:
    return _utc(point).date() == _utc(cutoff).date()


def _clamp_today_session_start(
    start: datetime,
    *,
    user_id: UUID,
    owner_id: UUID,
    cutoff: datetime,
    day_start: datetime,
    day_end: datetime,
) -> datetime:
    if user_id != owner_id:
        return start
    if _utc(day_start) <= cutoff < _utc(day_end):
        return max(_utc(start), cutoff)
    return start


def _virtual_balance(
    baseline: Decimal,
    realised_pnl: Decimal,
    open_profit: Decimal = _ZERO,
) -> tuple[Decimal, Decimal]:
    balance = baseline + realised_pnl
    equity = balance + open_profit
    return balance, equity


def _zero_window(key: str, label: str):
    from app.performance_ledger_day33 import Day33PerformanceWindow

    return Day33PerformanceWindow(
        key=key,
        label=label,
        cash_pnl=_ZERO,
        return_percent=None,
        model_500_pnl=_ZERO,
        model_500_return_percent=_ZERO,
        closed_trades=0,
        wins=0,
        losses=0,
        breakeven=0,
        open_trades=0,
        win_rate_percent=None,
        net_pips=None,
        mixed_instrument_pips=False,
    )


def install_paper_fresh_run_reset() -> bool:
    """Install the Owner paper-run boundary once when configured."""
    global _installed

    install_telegram_entity_recovery()

    if _installed:
        return True

    cutoff = paper_reset_at()
    owner_id = paper_owner_id()
    if cutoff is None or owner_id is None:
        return False

    from app.admin_portfolio_day35 import Day35AdminPortfolioService
    from app.dashboard_day32 import Day32DashboardService
    from app.dashboard_today_summary import TodayTradingSummaryService
    from app.performance_ledger_day33 import MODEL_BALANCE, _d, _money, _pct
    from app.performance_ledger_day33_v2 import Day33PerformanceLedgerServiceV2

    original_dashboard_read = Day32DashboardService.read
    original_windows = Day33PerformanceLedgerServiceV2.read_windows
    original_timeline_rows = Day33PerformanceLedgerServiceV2._timeline_rows
    original_mapped_open_positions = Day32DashboardService._mapped_open_positions
    original_latest_signal = Day32DashboardService._latest_signal
    original_recent_completed = Day32DashboardService._recent_completed
    original_activity = Day32DashboardService._activity
    original_period_start = Day35AdminPortfolioService._period_start
    original_today_session_start = TodayTradingSummaryService._session_start

    def eligible_signal_ids(self, user_id: UUID) -> set[UUID]:
        with self._session_factory() as session:
            return set(
                session.scalars(
                    text(
                        """
                        SELECT DISTINCT s.id
                        FROM signals s
                        LEFT JOIN positions p ON p.signal_id=s.id
                        WHERE COALESCE(s.source_posted_at,s.created_at)>=:cutoff
                          AND (p.user_id=:user_id OR p.user_id IS NULL)
                        """
                    ),
                    {"user_id": user_id, "cutoff": cutoff},
                ).all()
            )

    def post_reset_realised_cash(self, user_id: UUID) -> Decimal:
        with self._session_factory() as session:
            value = session.execute(
                text(
                    """
                    SELECT COALESCE(SUM(o.cash_pnl),0)
                    FROM performance_trade_outcomes o
                    JOIN signals s ON s.id=o.signal_id
                    WHERE o.user_id=:user_id
                      AND COALESCE(s.source_posted_at,s.created_at)>=:cutoff
                      AND o.status IN ('won','lost','breakeven')
                      AND o.cash_pnl IS NOT NULL
                    """
                ),
                {"user_id": user_id, "cutoff": cutoff},
            ).scalar_one()
        return _money(_d(value))

    def fresh_window(
        self,
        user_id: UUID,
        key: str,
        label: str,
        since: datetime,
        point: datetime,
    ):
        """Read one window using provider-signal start as the run boundary."""
        effective_since = max(_utc(since), cutoff)
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT o.status,o.symbol,o.cash_pnl,o.net_pips,o.model_500_pnl
                    FROM performance_trade_outcomes o
                    JOIN signals s ON s.id=o.signal_id
                    WHERE o.user_id=:user_id
                      AND COALESCE(s.source_posted_at,s.created_at)>=:cutoff
                      AND COALESCE(s.source_posted_at,s.created_at)<:point
                      AND (
                            (o.status IN ('won','lost','breakeven','closed_unknown')
                             AND o.closed_at>=:since
                             AND o.closed_at<:point)
                         OR (o.status IN ('open','pending')
                             AND COALESCE(o.opened_at,s.source_posted_at,s.created_at)>=:since
                             AND COALESCE(o.opened_at,s.source_posted_at,s.created_at)<:point)
                      )
                    """
                ),
                {
                    "user_id": user_id,
                    "cutoff": cutoff,
                    "since": effective_since,
                    "point": point,
                },
            ).mappings().all()

        known = [
            row
            for row in rows
            if row["status"] in {"won", "lost", "breakeven"}
            and row["cash_pnl"] is not None
        ]
        cash = _money(sum((_d(row["cash_pnl"]) for row in known), _ZERO))
        model = _money(
            sum(
                (
                    _d(row["model_500_pnl"])
                    for row in known
                    if row["model_500_pnl"] is not None
                ),
                _ZERO,
            )
        )
        wins = sum(1 for row in known if row["status"] == "won")
        losses = sum(1 for row in known if row["status"] == "lost")
        breakeven = sum(1 for row in known if row["status"] == "breakeven")
        decided = wins + losses
        win_rate = (
            _pct(Decimal(wins) / Decimal(decided) * Decimal("100"))
            if decided
            else None
        )
        symbols = {
            str(row["symbol"])
            for row in known
            if row["net_pips"] is not None
        }
        mixed = len(symbols) > 1
        net_pips = None
        if known and not mixed and all(row["net_pips"] is not None for row in known):
            net_pips = _money(sum((_d(row["net_pips"]) for row in known), _ZERO))

        from app.performance_ledger_day33 import Day33PerformanceWindow

        return Day33PerformanceWindow(
            key=key,
            label=label,
            cash_pnl=cash,
            return_percent=(
                _pct(cash / paper_baseline_balance() * Decimal("100"))
                if paper_baseline_balance() > 0
                else None
            ),
            model_500_pnl=model,
            model_500_return_percent=_pct(model / MODEL_BALANCE * Decimal("100")),
            closed_trades=len(known),
            wins=wins,
            losses=losses,
            breakeven=breakeven,
            open_trades=sum(1 for row in rows if row["status"] in {"open", "pending"}),
            win_rate_percent=win_rate,
            net_pips=net_pips,
            mixed_instrument_pips=mixed,
        )

    def reset_windows(self, user_id: UUID, *, now: datetime | None = None):
        if user_id != owner_id:
            return original_windows(self, user_id, now=now)
        point = _utc(now or datetime.now(UTC))
        today_start = max(point.replace(hour=0, minute=0, second=0, microsecond=0), cutoff)
        today_window = fresh_window(self, user_id, "today", "Today", today_start, point)

        if _same_reset_day(point, cutoff):
            return (
                today_window,
                _zero_window("7d", "7 days"),
                _zero_window("30d", "30 days"),
                _zero_window("month", "Month"),
                _zero_window("all", "All time"),
            )

        month = point.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        requested = (
            ("7d", "7 days", point - timedelta(days=7)),
            ("30d", "30 days", point - timedelta(days=30)),
            ("month", "Month", month),
            ("all", "All time", cutoff),
        )
        return (
            today_window,
            *(fresh_window(self, user_id, key, label, start, point) for key, label, start in requested),
        )

    def reset_timeline_rows(self, user_id: UUID):
        rows = original_timeline_rows(self, user_id)
        if user_id != owner_id or not rows:
            return rows
        eligible = eligible_signal_ids(self, user_id)
        return [row for row in rows if row.get("signal_id") in eligible]

    def reset_mapped_open_positions(self, user_id: UUID, broker_positions):
        values = original_mapped_open_positions(self, user_id, broker_positions)
        if user_id != owner_id or not values:
            return values
        eligible = eligible_signal_ids(self, user_id)
        return tuple(item for item in values if item.signal_id in eligible)

    def reset_latest_signal(self, user_id: UUID):
        value = original_latest_signal(self, user_id)
        if user_id != owner_id or value is None:
            return value
        eligible = eligible_signal_ids(self, user_id)
        return value if value.signal_id in eligible else None

    def reset_recent_completed(self, user_id: UUID):
        values = original_recent_completed(self, user_id)
        if user_id != owner_id or not values:
            return values
        eligible = eligible_signal_ids(self, user_id)
        return tuple(item for item in values if item.signal_id in eligible)

    def reset_activity(self, user_id: UUID):
        values = original_activity(self, user_id)
        if user_id != owner_id:
            return values
        return tuple(item for item in values if _after(item.created_at, cutoff))

    async def reset_dashboard_read(self, user_id: UUID):
        view = await original_dashboard_read(self, user_id)
        if user_id != owner_id or view.account is None:
            return view

        realised = post_reset_realised_cash(self, user_id)
        open_profit = _money(
            sum(
                (
                    Decimal(str(item.profit))
                    for item in view.open_positions
                    if item.profit is not None
                ),
                _ZERO,
            )
        )
        balance, equity = _virtual_balance(
            paper_baseline_balance(),
            realised,
            open_profit,
        )
        return replace(
            view,
            account=replace(
                view.account,
                balance=float(balance),
                equity=float(equity),
                margin=0.0,
                free_margin=float(equity),
            ),
            open_profit=float(open_profit),
        )

    def reset_period_start(period, now):
        start, label = original_period_start(period, now)
        return max(_utc(start), cutoff), label

    def reset_today_session_start(self, session, user_id: UUID, *, day_start, day_end):
        start = original_today_session_start(
            self,
            session,
            user_id,
            day_start=day_start,
            day_end=day_end,
        )
        return _clamp_today_session_start(
            start,
            user_id=user_id,
            owner_id=owner_id,
            cutoff=cutoff,
            day_start=day_start,
            day_end=day_end,
        )

    Day33PerformanceLedgerServiceV2.read_windows = reset_windows
    Day33PerformanceLedgerServiceV2._timeline_rows = reset_timeline_rows
    Day32DashboardService._mapped_open_positions = reset_mapped_open_positions
    Day32DashboardService._latest_signal = reset_latest_signal
    Day32DashboardService._recent_completed = reset_recent_completed
    Day32DashboardService._activity = reset_activity
    Day32DashboardService.read = reset_dashboard_read
    Day35AdminPortfolioService._period_start = staticmethod(reset_period_start)
    TodayTradingSummaryService._session_start = reset_today_session_start

    _installed = True
    return True


__all__ = [
    "install_paper_fresh_run_reset",
    "paper_baseline_balance",
    "paper_reset_at",
    "paper_owner_id",
]
