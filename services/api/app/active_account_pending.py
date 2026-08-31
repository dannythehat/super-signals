"""Pending-order reconciliation for the active Demo/Real account model."""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import text

from app.pending_reconciliation_canonical import AccountPendingReconciler, PendingReconcileResult
from app.unified_pending_reconciler import LiveMemberPendingReconciler, UnifiedPendingReconciler

logger = logging.getLogger(__name__)


class ActiveAccountPendingReconciler(AccountPendingReconciler):
    """Owner/reference pending truth follows whichever canonical account is active."""

    account_environment = "active"

    def _broker_account(self) -> tuple[str, bytes] | None:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT metaapi_account_id, metaapi_token_ciphertext
                    FROM mt5_accounts
                    WHERE owner_user_id=:user_id
                      AND status='connected'
                      AND account_environment IN ('demo','live')
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {"user_id": self._owner_user_id},
            ).mappings().first()
        if row is None:
            return None
        return str(row["metaapi_account_id"]), bytes(row["metaapi_token_ciphertext"])


class ActiveUnifiedPendingReconciler(UnifiedPendingReconciler):
    """Reconcile all pending exposure, regardless of active Demo/Real environment."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._owner = ActiveAccountPendingReconciler(
            session_factory=self._session_factory,
            cipher=self._cipher,
            gateway=self._gateway,
            owner_user_id=self._owner_user_id,
            poll_seconds=self._poll_seconds,
        )

    async def reconcile_once(self) -> PendingReconcileResult:
        owner_environment = self._active_environment(self._owner_user_id)
        if self._supersession_guard is not None and owner_environment is not None:
            cleanup = await self._supersession_guard.cleanup_existing(
                user_id=self._owner_user_id,
                account_environment=owner_environment,
            )
            if cleanup.cancelled or cleanup.terminalized:
                logger.warning(
                    "Superseded pending cleanup environment=%s cancelled=%d terminalized=%d unresolved=%d",
                    owner_environment,
                    cleanup.cancelled,
                    cleanup.terminalized,
                    cleanup.unresolved,
                )

        results = [await self._owner.reconcile_once()]
        for user_id, environment in self._member_pending_users():
            if self._supersession_guard is not None:
                cleanup = await self._supersession_guard.cleanup_existing(
                    user_id=user_id,
                    account_environment=environment,
                )
                if cleanup.cancelled or cleanup.terminalized:
                    logger.warning(
                        "Superseded pending cleanup environment=%s user=%s cancelled=%d terminalized=%d unresolved=%d",
                        environment,
                        user_id,
                        cleanup.cancelled,
                        cleanup.terminalized,
                        cleanup.unresolved,
                    )

            reconciler = (
                AccountPendingReconciler(
                    session_factory=self._session_factory,
                    cipher=self._cipher,
                    gateway=self._gateway,
                    owner_user_id=user_id,
                    poll_seconds=self._poll_seconds,
                )
                if environment == "demo"
                else LiveMemberPendingReconciler(
                    session_factory=self._session_factory,
                    cipher=self._cipher,
                    gateway=self._gateway,
                    owner_user_id=user_id,
                    poll_seconds=self._poll_seconds,
                )
            )
            results.append(await reconciler.reconcile_once())

        return PendingReconcileResult(
            pending_seen=sum(item.pending_seen for item in results),
            fills_mapped=sum(item.fills_mapped for item in results),
            still_pending=sum(item.still_pending for item in results),
            unresolved=sum(item.unresolved for item in results),
            terminalized=sum(item.terminalized for item in results),
        )

    def _active_environment(self, user_id: UUID) -> str | None:
        with self._session_factory() as session:
            value = session.execute(
                text(
                    """
                    SELECT lower(account_environment)
                    FROM mt5_accounts
                    WHERE owner_user_id=:user_id AND status='connected'
                    LIMIT 1
                    """
                ),
                {"user_id": user_id},
            ).scalar_one_or_none()
        environment = str(value or "")
        return environment if environment in {"demo", "live"} else None

    def _member_pending_users(self) -> tuple[tuple[UUID, str], ...]:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT DISTINCT p.user_id, lower(m.account_environment) AS account_environment
                    FROM positions AS p
                    JOIN users AS u ON u.id=p.user_id AND u.status='active'
                    JOIN user_roles AS ur ON ur.user_id=u.id
                    JOIN roles AS r ON r.id=ur.role_id AND r.name='user'
                    JOIN mt5_accounts AS m
                      ON m.owner_user_id=p.user_id
                     AND m.status='connected'
                    WHERE p.status='pending'
                      AND p.user_id != :owner_user_id
                      AND lower(m.account_environment) IN ('demo','live')
                    ORDER BY p.user_id
                    """
                ),
                {"owner_user_id": self._owner_user_id},
            ).mappings().all()
        return tuple(
            (UUID(str(row["user_id"])), str(row["account_environment"]))
            for row in rows
        )


__all__ = ["ActiveUnifiedPendingReconciler"]
