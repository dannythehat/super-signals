"""Production metrics consistency hotfix for connected member MT5 accounts.

This module is intentionally narrow:
* LIVE account Balance/Equity remain broker truth;
* aggregate Open P/L falls back to broker Equity - Balance when a partial position
  snapshot leaves one or more local open legs without a live profit value;
* every connected member account gets a read-only canonical performance sync so
  Today / This month / All-time realised values are populated from broker deal
  history, not only the reference/owner account.

No broker trade, close, modify, or pending-order action is created here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from uuid import UUID

from sqlalchemy import text

from app import broker_settlement_canonical as _broker_settlement_module
from app import dashboard_resilient_runtime as _dashboard_resilient_module
from app.broker_settlement_canonical import CanonicalBrokerSettlementManager as _BaseSettlementManager
from app.dashboard_day32 import Day32DashboardService, Day32DashboardView
from app.dashboard_resilient_runtime import (
    ResilientDashboardRuntimeService as _BaseResilientDashboardRuntimeService,
)
from app.performance_ledger_day33 import Day33LedgerError

logger = logging.getLogger(__name__)

_MEMBER_METRICS_SYNC_SECONDS = 60.0


class MemberMetricsResilientDashboardRuntimeService(
    _BaseResilientDashboardRuntimeService
):
    """LIVE-member dashboard variant that preserves broker account truth."""

    async def _read_live_and_cache(self, user_id: UUID) -> Day32DashboardView:
        # Keep the original test/fallback behaviour for deliberately half-built
        # instances and keep demo/paper accounting completely unchanged.
        if not hasattr(self, "_session_factory"):
            return await super()._read_live_and_cache(user_id)

        account_row = self._account_row(user_id)
        if (
            account_row is None
            or str(account_row["account_environment"] or "").strip().lower() != "live"
        ):
            return await super()._read_live_and_cache(user_id)

        # For LIVE accounts bypass the synthetic/canonical demo equity transform and
        # preserve the account values returned by MetaAPI exactly.
        view = await Day32DashboardService.read(self, user_id)
        if view.account is not None:
            self._persist_last_confirmed_account(
                user_id,
                view.account,
                read_at=view.connection.read_at or self._utc_now(),
            )
            if view.connection.status == "connected":
                self._last_views[user_id] = view
            return view

        if not view.connection.configured or view.connection.status != "connection_error":
            return view
        cached = self._last_confirmed_account(user_id)
        if cached is None:
            return view
        return replace(view, account=cached)

    @staticmethod
    def _utc_now():  # noqa: ANN205
        from datetime import UTC, datetime

        return datetime.now(UTC)

    def _refresh_cached_account(
        self,
        user_id: UUID,
        view: Day32DashboardView,
    ) -> Day32DashboardView:
        # The deployed branch has this demo-account refresh hook. LIVE accounts must
        # retain the broker-confirmed Balance/Equity/Free Margin instead of rebuilding
        # them from a potentially partial position snapshot.
        if str(view.connection.account_environment or "").strip().lower() == "live":
            return view
        parent = super()
        refresh = getattr(parent, "_refresh_cached_account", None)
        return refresh(user_id, view) if refresh is not None else view

    def _overlay_current_open_positions(
        self,
        user_id: UUID,
        view: Day32DashboardView,
    ) -> Day32DashboardView:
        current = super()._overlay_current_open_positions(user_id, view)
        if (
            str(current.connection.account_environment or "").strip().lower() != "live"
            or current.open_profit is not None
            or current.account is None
        ):
            return current

        # When one durable leg is absent from a position snapshot its individual P/L
        # is unknown, but the broker account still exposes the exact aggregate floating
        # result through Equity - Balance. Use that broker truth for the card total.
        return replace(
            current,
            open_profit=float(current.account.equity) - float(current.account.balance),
        )


class MemberMetricsBrokerSettlementManager(_BaseSettlementManager):
    """Canonical settlement plus read-only metrics sync for every connected account."""

    def _connected_member_user_ids(self) -> tuple[UUID, ...]:
        with self._session_factory() as session:  # noqa: SLF001
            rows = session.execute(
                text(
                    """
                    SELECT DISTINCT ON (owner_user_id) owner_user_id
                    FROM mt5_accounts
                    WHERE status='connected'
                      AND metaapi_account_id IS NOT NULL
                      AND metaapi_token_ciphertext IS NOT NULL
                    ORDER BY owner_user_id,created_at DESC
                    """
                )
            ).scalars().all()
        return tuple(UUID(str(value)) for value in rows)

    async def _sync_connected_member_metrics(self) -> None:
        now = time.monotonic()
        last_sync: dict[UUID, float] = getattr(
            self,
            "_member_metrics_last_sync",
            {},
        )
        self._member_metrics_last_sync = last_sync

        for user_id in self._connected_member_user_ids():
            # The canonical settlement path already owns the reference account.
            if user_id == self._reference_user_id:  # noqa: SLF001
                continue
            previous = float(last_sync.get(user_id, 0.0) or 0.0)
            if previous and now - previous < _MEMBER_METRICS_SYNC_SECONDS:
                continue
            try:
                await self._performance.sync_user(user_id)  # noqa: SLF001
            except Day33LedgerError as exc:
                logger.warning(
                    "Connected member metrics sync deferred user=%s code=%s retryable=%s",
                    user_id,
                    exc.code,
                    exc.retryable,
                )
                continue
            except Exception:
                # Metrics observability must never take down broker settlement/execution.
                logger.exception("Connected member metrics sync failed safely user=%s", user_id)
                continue
            last_sync[user_id] = time.monotonic()

    async def poll_once(self):  # noqa: ANN201
        """Preserve _has_unsettled_mapped_positions / sync_user / flat_account_truth_sync_complete.

        The parent method remains the settlement authority. This subclass adds only a
        read-only account/deal-history sync for the other connected members afterwards.
        """
        result = await super().poll_once()
        if not result.synced:
            return result
        try:
            await asyncio.wait_for(
                self._sync_connected_member_metrics(),
                timeout=5.0,
            )
        except TimeoutError:
            logger.warning(
                "Connected member metrics sync timed out safely; owner settlement loop continues"
            )
        return result


# Replace only the module-level service aliases that main.py/routes import after the
# package initializer completes. The canonical base classes themselves are untouched.
_broker_settlement_module.CanonicalBrokerSettlementManager = (
    MemberMetricsBrokerSettlementManager
)
_dashboard_resilient_module.ResilientDashboardRuntimeService = (
    MemberMetricsResilientDashboardRuntimeService
)


__all__ = [
    "MemberMetricsBrokerSettlementManager",
    "MemberMetricsResilientDashboardRuntimeService",
]
