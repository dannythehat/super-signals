"""Non-destructive fresh-run visibility boundary for Owner DEMO paper testing.

Old broker/deal/audit records remain intact for incident review. When
``SUPER_SIGNALS_PAPER_RESET_AT`` is configured, Owner-facing dashboard/performance
reads treat that UTC instant as the start of a new paper run. This removes old trades
from the app without deleting forensic evidence or changing broker truth.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.telegram_entity_recovery import install_telegram_entity_recovery

_installed = False


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


def _after(value: datetime | None, cutoff: datetime) -> bool:
    if value is None:
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC) >= cutoff


def install_paper_fresh_run_reset() -> bool:
    """Install Telegram recovery and Owner reset filters once when configured."""
    global _installed

    # This listener-reliability correction is independent of the display reset. The
    # Day38 builder always calls this startup hook, including while broker execution is
    # paused, so cold Telethon sessions can rebuild channel access hashes before gap
    # recovery is needed.
    install_telegram_entity_recovery()

    if _installed:
        return True

    cutoff = paper_reset_at()
    owner_id = paper_owner_id()
    if cutoff is None or owner_id is None:
        return False

    from app.admin_portfolio_day35 import Day35AdminPortfolioService
    from app.dashboard_day32 import Day32DashboardService
    from app.performance_ledger_day33_v2 import Day33PerformanceLedgerServiceV2

    original_windows = Day33PerformanceLedgerServiceV2.read_windows
    original_timeline_rows = Day33PerformanceLedgerServiceV2._timeline_rows
    original_latest_signal = Day32DashboardService._latest_signal
    original_recent_completed = Day32DashboardService._recent_completed
    original_period_start = Day35AdminPortfolioService._period_start

    def reset_windows(self, user_id: UUID, *, now: datetime | None = None):
        if user_id != owner_id:
            return original_windows(self, user_id, now=now)
        point = now or datetime.now(UTC)
        if point.tzinfo is None:
            point = point.replace(tzinfo=UTC)
        point = point.astimezone(UTC)
        today = point.replace(hour=0, minute=0, second=0, microsecond=0)
        month = today.replace(day=1)
        requested = (
            ("today", "Today", today),
            ("7d", "7 days", point - timedelta(days=7)),
            ("30d", "30 days", point - timedelta(days=30)),
            ("month", "Month", month),
            ("all", "All time", cutoff),
        )
        return tuple(
            self._window(
                user_id,
                key,
                label,
                max(start, cutoff),
                point,
            )
            for key, label, start in requested
        )

    def reset_timeline_rows(self, user_id: UUID):
        rows = original_timeline_rows(self, user_id)
        if user_id != owner_id:
            return rows
        result = []
        for row in rows:
            value = row.get("sort_at")
            if value is None:
                value = row.get("opened_at") or row.get("closed_at")
            if _after(value, cutoff):
                result.append(row)
        return result

    def reset_latest_signal(self, user_id: UUID):
        value = original_latest_signal(self, user_id)
        if user_id != owner_id or value is None:
            return value
        return value if _after(value.created_at, cutoff) else None

    def reset_recent_completed(self, user_id: UUID):
        values = original_recent_completed(self, user_id)
        if user_id != owner_id:
            return values
        return tuple(item for item in values if _after(item.closed_at, cutoff))

    def reset_period_start(period, now):
        start, label = original_period_start(period, now)
        return max(start, cutoff), label

    Day33PerformanceLedgerServiceV2.read_windows = reset_windows
    Day33PerformanceLedgerServiceV2._timeline_rows = reset_timeline_rows
    Day32DashboardService._latest_signal = reset_latest_signal
    Day32DashboardService._recent_completed = reset_recent_completed
    Day35AdminPortfolioService._period_start = staticmethod(reset_period_start)

    _installed = True
    return True


__all__ = ["install_paper_fresh_run_reset", "paper_reset_at"]
