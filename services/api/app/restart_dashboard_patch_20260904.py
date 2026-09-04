"""Runtime reporting guard for the 4 Sep 2026 11:00 Sofia clean restart.

The canonical Today service originally used local midnight (or a material balance reset)
for trade counts even when a stronger clean-restart reporting override existed. It also
adds reviewed override cash to post-cutoff outcome cash. The Owner intentionally retains
the USD -3.60 cleanup settlement in the reviewed restart row, while that same cleanup
settlement is also a real post-cutoff close in immutable outcomes.

This guard makes Today counts begin at the restart cutoff and removes that one duplicate
reviewed-cash addition from the Today strip only. Canonical broker accounting continues to
use the reviewed -3.60 row, so Today/week/month/all-time remain aligned.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from app.restart_20260904_1100 import RESTART_CUTOFF

# 5 Sep 2026 00:00 Europe/Sofia = 4 Sep 2026 21:00 UTC.
_RESTART_DAY_END = datetime(2026, 9, 4, 21, 0, 0, tzinfo=UTC)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def install_restart_dashboard_patch() -> None:
    from app import dashboard_today_summary as today

    service_cls = today.TodayTradingSummaryService
    current_session_start = service_cls._session_start
    current_read = service_cls.read
    if getattr(current_read, "_ss_restart_1100_dashboard_patch", False):
        return

    def restart_session_start(
        self: Any,
        session: Any,
        user_id: Any,
        *,
        day_start: datetime,
        day_end: datetime,
    ) -> datetime:
        start = current_session_start(
            self,
            session,
            user_id,
            day_start=day_start,
            day_end=day_end,
        )
        restart_cutoff = session.execute(
            text(
                """
                SELECT cutoff_at
                FROM performance_reporting_overrides
                WHERE user_id=:user_id
                  AND incident_key LIKE 'restart-2026-09-04-1100-%'
                  AND cutoff_at>=:day_start
                  AND cutoff_at<:day_end
                LIMIT 1
                """
            ),
            {
                "user_id": user_id,
                "day_start": _utc(day_start),
                "day_end": _utc(day_end),
            },
        ).scalar_one_or_none()
        if restart_cutoff is None:
            return start
        return max(_utc(start), _utc(restart_cutoff))

    def restart_read(
        self: Any,
        user_id: Any,
        *,
        timezone_name: str = "UTC",
        now_utc: datetime | None = None,
    ):
        result = current_read(
            self,
            user_id,
            timezone_name=timezone_name,
            now_utc=now_utc,
        )
        point = _utc(now_utc or datetime.now(UTC))
        if not (RESTART_CUTOFF <= point < _RESTART_DAY_END):
            return result

        with self._session_factory() as session:
            reviewed_cash = session.execute(
                text(
                    """
                    SELECT realised_cash_pnl
                    FROM performance_reporting_overrides
                    WHERE user_id=:user_id
                      AND incident_key LIKE 'restart-2026-09-04-1100-%'
                      AND cutoff_at=:cutoff
                    LIMIT 1
                    """
                ),
                {"user_id": user_id, "cutoff": RESTART_CUTOFF},
            ).scalar_one_or_none()
        if reviewed_cash is None:
            return result

        # TodayTradingSummaryService already adds the reviewed cash to every outcome
        # closed after the cutoff. For the restart row, the retained -3.60 settlement
        # is itself one of those immutable post-cutoff outcomes, so subtract the
        # reviewed amount once to prevent double counting in this strip.
        reviewed = Decimal(str(reviewed_cash or 0))
        return replace(result, realised_pnl=result.realised_pnl - reviewed)

    restart_read._ss_restart_1100_dashboard_patch = True  # type: ignore[attr-defined]
    service_cls._session_start = restart_session_start
    service_cls.read = restart_read
    print("SUPER_SIGNALS_RESTART_1100_DASHBOARD_PATCH=ACTIVE", flush=True)


__all__ = ["install_restart_dashboard_patch"]
