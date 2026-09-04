"""Restore the authoritative 4 Sep 2026 11:00 Sofia clean-restart reporting rows.

The morning Asia incident cleanup and the 11:00 clean restart shared the historical
(user_id, reporting_date) override slot. A later process restart could therefore replace
the clean restart row with the retired incident quarantine. This repair makes the clean
restart authoritative for every account connected at the boundary.

The Owner intentionally retains the USD -3.60 broker cleanup settlement agreed after the
restart. Other connected users retain a zero reviewed carry at the boundary. Immutable
broker deals/outcomes are never changed.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import text

from app.db import get_session_factory
from app.restart_20260904_1100 import REPORTING_DATE, RESTART_CUTOFF, TIMEZONE

OWNER_RETAINED_CLEANUP_CASH = Decimal("-3.60")


def restore_restart_overrides() -> None:
    session_factory = get_session_factory()
    with session_factory() as session:
        owner_user_id = session.execute(
            text(
                """
                SELECT u.id
                FROM users u
                JOIN user_roles ur ON ur.user_id=u.id
                JOIN roles r ON r.id=ur.role_id
                WHERE r.name='owner'
                ORDER BY u.created_at
                LIMIT 1
                """
            )
        ).scalar_one_or_none()
        user_ids = session.execute(
            text(
                """
                SELECT DISTINCT m.owner_user_id
                FROM mt5_accounts m
                JOIN users u ON u.id=m.owner_user_id
                WHERE m.status='connected'
                  AND u.status NOT IN ('revoked','suspended')
                ORDER BY m.owner_user_id
                """
            )
        ).scalars().all()

        for user_id in user_ids:
            retained_cash = (
                OWNER_RETAINED_CLEANUP_CASH
                if owner_user_id is not None and user_id == owner_user_id
                else Decimal("0.00")
            )
            reason = (
                "4 September 2026 clean restart boundary at 11:00 Europe/Sofia. "
                "All trading activity opened before the boundary is excluded from "
                "user-facing performance and trade history. Immutable broker evidence "
                "is preserved. Only trades opened at or after 11:00 Europe/Sofia count "
                "forward."
            )
            if retained_cash != 0:
                reason += (
                    " Owner reporting intentionally retains the USD -3.60 clean-restart "
                    "broker settlement by explicit product-owner decision."
                )
            incident_key = f"restart-2026-09-04-1100-{str(user_id).replace('-', '')[:32]}"
            session.execute(
                text(
                    """
                    INSERT INTO performance_reporting_overrides
                        (user_id, reporting_date, timezone, realised_cash_pnl,
                         reason, incident_key, cutoff_at, created_at, updated_at)
                    VALUES
                        (:user_id, :reporting_date, :timezone, :cash,
                         :reason, :incident_key, :cutoff, now(), now())
                    ON CONFLICT (user_id, reporting_date) DO UPDATE
                    SET timezone=EXCLUDED.timezone,
                        realised_cash_pnl=EXCLUDED.realised_cash_pnl,
                        reason=EXCLUDED.reason,
                        incident_key=EXCLUDED.incident_key,
                        cutoff_at=EXCLUDED.cutoff_at,
                        updated_at=now()
                    """
                ),
                {
                    "user_id": user_id,
                    "reporting_date": REPORTING_DATE,
                    "timezone": TIMEZONE,
                    "cash": retained_cash,
                    "reason": reason,
                    "incident_key": incident_key,
                    "cutoff": RESTART_CUTOFF,
                },
            )
        session.commit()

    print(
        "SUPER_SIGNALS_RESTART_1100_OVERRIDE_REPAIR=PASS "
        f"connected={len(user_ids)} owner_retained_cash={OWNER_RETAINED_CLEANUP_CASH}",
        flush=True,
    )


__all__ = ["OWNER_RETAINED_CLEANUP_CASH", "restore_restart_overrides"]
