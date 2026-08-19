"""Non-destructive fresh-run visibility boundary for Owner DEMO paper testing.

Raw broker deals, audit events and historical outcomes remain intact as immutable evidence.
When ``SUPER_SIGNALS_PAPER_RESET_AT`` is configured, Owner-facing reads expose only the
new paper-test run from that UTC instant onward. Current broker balance/equity and genuine
open broker exposure are never rewritten or hidden by this module.

Reset-day display semantics are deliberate:
- Today remains the live Today figure the user was already watching.
- 7 days / 30 days / Month / All time are explicit zeroes on the reset day.
- From the following day those historical windows accumulate only signals whose provider
  event started at or after the reset boundary.
- Trade/history lists exclude pre-reset signals even if an old position happened to close
  after the reset boundary.
- The live Today strip uses the reset instant as its session start, so old pending/trade
  counts cannot leak into the new run.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text

from app.telegram_entity_recovery import install_telegram_entity_recovery

_installed = False
_ZERO = Decimal("0")


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
    """Install the Owner paper-run visibility boundary once when configured."""
    global _installed

    # Listener reliability is independent of the display reset. The production builder
    # always calls this hook, so cold Telethon sessions can recover channel entities.
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

    original_windows = Day33PerformanceLedgerServiceV2.read_windows
    original_timeline_rows = Day33PerformanceLedgerServiceV2._timeline_rows
    original_latest_signal = Day32DashboardService._latest_signal
    original_recent_completed = Day32DashboardService._recent_completed
    original_activity = Day32DashboardService._activity
    original_period_start = Day35AdminPortfolioService._period_start
    original_today_session_start = TodayTradingSummaryService._session_start

    def fresh_window(self, user_id: UUID, key: str, label: str, since: datetime, point: datetime):
        """Read one post-reset window by provider signal start, not merely close time."""
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
        return_percent = self._period_return_percent(user_id, cutoff, cash)

        from app.performance_ledger_day33 import Day33PerformanceWindow

        return Day33PerformanceWindow(
            key=key,
            label=label,
            cash_pnl=cash,
            return_percent=return_percent,
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

        # Preserve the live Today figure exactly as the existing dashboard calculates it.
        # This was explicitly the user's trusted current-day number at reset time.
        current = original_windows(self, user_id, now=point)
        today_window = next((item for item in current if item.key == "today"), None)
        if today_window is None:
            today_window = _zero_window("today", "Today")

        if _same_reset_day(point, cutoff):
            return (
                today_window,
                _zero_window("7d", "7 days"),
                _zero_window("30d", "30 days"),
                _zero_window("month", "Month"),
                _zero_window("all", "All time"),
            )

        today = point.replace(hour=0, minute=0, second=0, microsecond=0)
        month = today.replace(day=1)
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
        with self._session_factory() as session:
            eligible = set(
                session.scalars(
                    text(
                        """
                        SELECT id
                        FROM signals
                        WHERE COALESCE(source_posted_at,created_at)>=:cutoff
                        """
                    ),
                    {"cutoff": cutoff},
                ).all()
            )
        return [row for row in rows if row.get("signal_id") in eligible]

    def reset_latest_signal(self, user_id: UUID):
        value = original_latest_signal(self, user_id)
        if user_id != owner_id or value is None:
            return value
        return value if _after(value.created_at, cutoff) else None

    def reset_recent_completed(self, user_id: UUID):
        values = original_recent_completed(self, user_id)
        if user_id != owner_id or not values:
            return values
        with self._session_factory() as session:
            eligible = set(
                session.scalars(
                    text(
                        """
                        SELECT p.id
                        FROM positions p
                        JOIN signals s ON s.id=p.signal_id
                        WHERE p.user_id=:user_id
                          AND COALESCE(s.source_posted_at,s.created_at)>=:cutoff
                        """
                    ),
                    {"user_id": user_id, "cutoff": cutoff},
                ).all()
            )
        return tuple(item for item in values if item.position_id in eligible)

    def reset_activity(self, user_id: UUID):
        values = original_activity(self, user_id)
        if user_id != owner_id:
            return values
        return tuple(item for item in values if _after(item.created_at, cutoff))

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
    Day32DashboardService._latest_signal = reset_latest_signal
    Day32DashboardService._recent_completed = reset_recent_completed
    Day32DashboardService._activity = reset_activity
    Day35AdminPortfolioService._period_start = staticmethod(reset_period_start)
    TodayTradingSummaryService._session_start = reset_today_session_start

    _installed = True
    return True


__all__ = [
    "install_paper_fresh_run_reset",
    "paper_reset_at",
    "paper_owner_id",
]
