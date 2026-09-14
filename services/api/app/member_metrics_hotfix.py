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

import logging
import time
from dataclasses import replace
from uuid import UUID

from sqlalchemy import text

from app.broker_settlement_canonical import CanonicalBrokerSettlementManager
from app.dashboard_day32 import Day32DashboardService, Day32DashboardView
from app.dashboard_resilient_runtime import ResilientDashboardRuntimeService
from app.dashboard_runtime import CanonicalDashboardRuntimeService
from app.performance_ledger_day33 import Day33LedgerError
from app.trading_accounting import CanonicalTradingAccountingService

logger = logging.getLogger(__name__)

_MEMBER_METRICS_SYNC_SECONDS = 60.0


# --- LIVE account Balance / Equity must remain broker truth -----------------

_original_canonical_dashboard_read = CanonicalDashboardRuntimeService.read


async def _canonical_dashboard_read_with_live_broker_truth(
    self: CanonicalDashboardRuntimeService,
    user_id: UUID,
) -> Day32DashboardView:
    """Preserve raw broker account values for LIVE accounts.

    The synthetic balance/equity transformation exists only for the owner paper/demo
    epoch. Applying it to LIVE accounts can make Equity disagree with the broker when
    a position snapshot is partial. LIVE accounts therefore return the broker account
    values from Day32 unchanged.
    """
    accounting = CanonicalTradingAccountingService(self._session_factory)
    if not accounting.uses_synthetic_demo_balance(user_id):
        return await Day32DashboardService.read(self, user_id)
    return await _original_canonical_dashboard_read(self, user_id)


CanonicalDashboardRuntimeService.read = _canonical_dashboard_read_with_live_broker_truth


# --- Aggregate Open P/L must never become a dash while broker account truth exists ---

_original_overlay_current_open_positions = (
    ResilientDashboardRuntimeService._overlay_current_open_positions
)


def _overlay_current_open_positions_with_account_fallback(
    self: ResilientDashboardRuntimeService,
    user_id: UUID,
    view: Day32DashboardView,
) -> Day32DashboardView:
    current = _original_overlay_current_open_positions(self, user_id, view)
    if current.open_profit is not None or current.account is None:
        return current

    # Broker Equity - Balance is the account-level floating P/L truth. Use it only
    # when one or more durable local legs have unknown per-position profit because
    # the latest position snapshot was partial.
    return replace(
        current,
        open_profit=float(current.account.equity) - float(current.account.balance),
    )


ResilientDashboardRuntimeService._overlay_current_open_positions = (
    _overlay_current_open_positions_with_account_fallback
)


# --- Realised metrics sync for every connected account ---------------------

_original_settlement_poll_once = CanonicalBrokerSettlementManager.poll_once


def _connected_member_user_ids(
    manager: CanonicalBrokerSettlementManager,
) -> tuple[UUID, ...]:
    with manager._session_factory() as session:  # noqa: SLF001
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


async def _sync_connected_member_metrics(
    manager: CanonicalBrokerSettlementManager,
) -> None:
    now = time.monotonic()
    last_sync: dict[UUID, float] = getattr(
        manager,
        "_member_metrics_last_sync",
        {},
    )
    manager._member_metrics_last_sync = last_sync  # type: ignore[attr-defined]  # noqa: SLF001

    for user_id in _connected_member_user_ids(manager):
        # The canonical settlement path already owns the reference account.
        if user_id == manager._reference_user_id:  # noqa: SLF001
            continue
        previous = float(last_sync.get(user_id, 0.0) or 0.0)
        if previous and now - previous < _MEMBER_METRICS_SYNC_SECONDS:
            continue
        try:
            await manager._performance.sync_user(user_id)  # noqa: SLF001
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


async def _settlement_poll_once_with_member_metrics(
    self: CanonicalBrokerSettlementManager,
):  # noqa: ANN201
    result = await _original_settlement_poll_once(self)
    await _sync_connected_member_metrics(self)
    return result


CanonicalBrokerSettlementManager.poll_once = _settlement_poll_once_with_member_metrics


__all__ = []
