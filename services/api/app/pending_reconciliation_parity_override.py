"""Extend the always-on pending reconciler to LIVE members when the live switch is on.

The listener still owns one reconciliation loop. This override keeps that operational
shape while making the loop use the exact same pending-fill mapping algorithm for every
approved LIVE member account as for the Owner DEMO account.
"""

from __future__ import annotations

import os
from uuid import UUID

from sqlalchemy import text

from app.paper_pending_reconciler import PaperPendingReconcileResult, PaperPendingReconciler
from app.unified_pending_reconciler import LiveMemberPendingReconciler

_installed = False


def _live_enabled() -> bool:
    return os.getenv("SUPER_SIGNALS_LIVE_EXECUTION_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def install_pending_reconciliation_parity_override() -> None:
    global _installed
    if _installed:
        return

    original = PaperPendingReconciler.reconcile_once
    if getattr(original, "_live_parity_enabled", False):
        _installed = True
        return

    async def reconcile_once(self: PaperPendingReconciler) -> PaperPendingReconcileResult:
        owner_result = await original(self)
        if not _live_enabled():
            return owner_result

        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT DISTINCT p.user_id
                    FROM positions AS p
                    JOIN users AS u ON u.id=p.user_id AND u.status='active'
                    JOIN user_roles AS ur ON ur.user_id=u.id
                    JOIN roles AS r ON r.id=ur.role_id AND r.name='user'
                    WHERE p.status='pending'
                      AND p.user_id != :owner_user_id
                    ORDER BY p.user_id
                    """
                ),
                {"owner_user_id": self._owner_user_id},
            ).scalars().all()

        results = [owner_result]
        for value in rows:
            member = LiveMemberPendingReconciler(
                session_factory=self._session_factory,
                cipher=self._cipher,
                gateway=self._gateway,
                owner_user_id=UUID(str(value)),
                poll_seconds=self._poll_seconds,
            )
            # Call the saved original directly so a member reconciliation does not
            # recursively fan out to every other member again.
            results.append(await original(member))

        return PaperPendingReconcileResult(
            pending_seen=sum(item.pending_seen for item in results),
            fills_mapped=sum(item.fills_mapped for item in results),
            still_pending=sum(item.still_pending for item in results),
            unresolved=sum(item.unresolved for item in results),
        )

    reconcile_once._live_parity_enabled = True  # type: ignore[attr-defined]
    PaperPendingReconciler.reconcile_once = reconcile_once
    _installed = True


__all__ = ["install_pending_reconciliation_parity_override"]
