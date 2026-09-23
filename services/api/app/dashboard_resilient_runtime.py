"""Persistent last-confirmed MT5 account fallback for the mobile dashboard.

This layer never invents broker state. A successful canonical dashboard read stores the
exact account values that were displayed. Dashboard requests are intentionally
stale-while-revalidate: the last confirmed view is returned immediately and broker
refreshes are heavily rate-limited so observability can never compete with trade
execution for MetaAPI capacity.

Dashboard observability is deliberately non-destructive. A successful-but-partial MetaAPI
position snapshot must never be allowed to close a real broker position or make it vanish
from the app. Durable locally mapped open positions remain visible until broker settlement
or an explicit trading action confirms that they are closed.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text

from app.dashboard_day32 import Day32Account, Day32Connection, Day32DashboardView
from app.dashboard_runtime import CanonicalDashboardRuntimeService
from app.trading_accounting import CanonicalTradingAccountingService

logger = logging.getLogger(__name__)

_HEALTHY_REFRESH_COOLDOWN_SECONDS = 60.0
_FAILED_REFRESH_BACKOFF_SECONDS = 600.0


class ResilientDashboardRuntimeService(CanonicalDashboardRuntimeService):
    """Canonical dashboard with durable and in-memory last-confirmed snapshots."""

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002,ANN003
        super().__init__(*args, **kwargs)
        self._last_views: dict[UUID, Day32DashboardView] = {}
        self._refresh_tasks: dict[UUID, asyncio.Task[None]] = {}
        self._refresh_not_before: dict[UUID, float] = {}

    def _ensure_runtime_state(self) -> None:
        if not hasattr(self, "_last_views"):
            self._last_views = {}
        if not hasattr(self, "_refresh_tasks"):
            self._refresh_tasks = {}
        if not hasattr(self, "_refresh_not_before"):
            self._refresh_not_before = {}

    def _reconcile_missing_open_positions(self, *, user_id: UUID, broker_position_ids: set[str], now: datetime) -> int:
        del user_id, broker_position_ids, now
        return 0

    def _mapped_open_positions(self, user_id: UUID, broker_positions):  # noqa: ANN001
        live = super()._mapped_open_positions(user_id, broker_positions)
        durable = self._durable_mapped_open_positions(user_id)
        live_by_broker_id = {item.broker_position_id: item for item in live}
        return tuple(live_by_broker_id.get(item.broker_position_id, item) for item in durable)

    def _overlay_current_open_positions(self, user_id: UUID, view: Day32DashboardView) -> Day32DashboardView:
        durable = self._durable_mapped_open_positions(user_id)
        cached_by_broker_id = {item.broker_position_id: item for item in view.open_positions}
        merged = tuple(cached_by_broker_id.get(item.broker_position_id, item) for item in durable)
        if not merged:
            open_profit: float | None = 0.0
        elif any(item.profit is None for item in merged):
            open_profit = None
        else:
            open_profit = sum(float(item.profit or 0.0) for item in merged)
        return replace(view, open_positions=merged, open_profit=open_profit, reconciled_external_positions=0)

    def _refresh_cached_account(self, user_id: UUID, view: Day32DashboardView) -> Day32DashboardView:
        """Keep the broker's MT5 balance authoritative on every cached dashboard read.

        Balance is closed broker cash. Equity and free margin are separate live broker
        fields and must never be copied into the balance or reconstructed from it.
        """
        if view.account is None:
            return view
        accounting = CanonicalTradingAccountingService(self._session_factory)
        broker_balance = float(
            accounting.displayed_balance(
                user_id, broker_account_value=view.account.balance
            )
        )
        account = replace(view.account, balance=broker_balance)
        return replace(view, account=account)

    @staticmethod
    def _now_ts() -> float:
        return datetime.now(UTC).timestamp()

    def _refresh_allowed(self, user_id: UUID) -> bool:
        return self._now_ts() >= self._refresh_not_before.get(user_id, 0.0)

    def _defer_refresh(self, user_id: UUID, seconds: float) -> None:
        self._refresh_not_before[user_id] = self._now_ts() + seconds

    async def read(self, user_id: UUID) -> Day32DashboardView:
        self._ensure_runtime_state()
        if not hasattr(self, "_session_factory"):
            return await self._read_live_and_cache(user_id)

        account_row = self._account_row(user_id)
        if account_row is None or str(account_row["status"]) != "connected":
            return await self._read_live_and_cache(user_id)

        cached = self._last_views.get(user_id)
        if cached is None:
            cached = self._durable_dashboard_snapshot(user_id, account_row=account_row)
            if cached is not None:
                self._last_views[user_id] = cached

        if cached is None:
            if not self._refresh_allowed(user_id):
                return self._local_reconnecting_snapshot(user_id, account_row=account_row)
            self._defer_refresh(user_id, _HEALTHY_REFRESH_COOLDOWN_SECONDS)
            view = await self._read_live_and_cache(user_id)
            if view.connection.status == "connected" and view.account is not None:
                self._defer_refresh(user_id, _HEALTHY_REFRESH_COOLDOWN_SECONDS)
            else:
                self._defer_refresh(user_id, _FAILED_REFRESH_BACKOFF_SECONDS)
            return self._refresh_cached_account(user_id, view)

        self._ensure_live_refresh(user_id)
        current = self._overlay_current_open_positions(user_id, cached)
        return self._refresh_cached_account(user_id, current)

    async def _read_live_and_cache(self, user_id: UUID) -> Day32DashboardView:
        self._ensure_runtime_state()
        view = await super().read(user_id)
        if view.account is not None:
            self._persist_last_confirmed_account(user_id, view.account, read_at=view.connection.read_at or datetime.now(UTC))
            if view.connection.status == "connected":
                self._last_views[user_id] = view
            return view
        if not view.connection.configured or view.connection.status != "connection_error":
            return view
        cached = self._last_confirmed_account(user_id)
        if cached is None:
            return view
        return replace(view, account=cached)

    def _ensure_live_refresh(self, user_id: UUID) -> None:
        self._ensure_runtime_state()
        current = self._refresh_tasks.get(user_id)
        if current is not None and not current.done():
            return
        if not self._refresh_allowed(user_id):
            return
        self._defer_refresh(user_id, _HEALTHY_REFRESH_COOLDOWN_SECONDS)
        self._refresh_tasks[user_id] = asyncio.create_task(self._refresh_live(user_id), name=f"dashboard-live-refresh-{user_id}")

    async def _refresh_live(self, user_id: UUID) -> None:
        try:
            view = await self._read_live_and_cache(user_id)
            if view.connection.status == "connected" and view.account is not None:
                self._last_views[user_id] = view
                self._defer_refresh(user_id, _HEALTHY_REFRESH_COOLDOWN_SECONDS)
            else:
                self._defer_refresh(user_id, _FAILED_REFRESH_BACKOFF_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._defer_refresh(user_id, _FAILED_REFRESH_BACKOFF_SECONDS)
            logger.exception("Background MT5 dashboard refresh failed safely")
        finally:
            self._refresh_tasks.pop(user_id, None)

    def _local_reconnecting_snapshot(self, user_id: UUID, *, account_row) -> Day32DashboardView:  # noqa: ANN001
        now = datetime.now(UTC)
        connection = Day32Connection(
            configured=True,
            status="reconnecting",
            account_environment=str(account_row["account_environment"]),
            login_masked=self._mask_login(str(account_row["login"])),
            server=str(account_row["server"]),
            error_code=None,
            read_at=None,
        )
        return self._without_live_state(connection=connection, trading=self._trading(user_id), user_id=user_id, now=now)

    def _durable_dashboard_snapshot(self, user_id: UUID, *, account_row) -> Day32DashboardView | None:  # noqa: ANN001
        snapshot = self._last_confirmed_account_with_time(user_id)
        if snapshot is None:
            return None
        account, read_at = snapshot
        now = datetime.now(UTC)
        connection = Day32Connection(
            configured=True,
            status="reconnecting",
            account_environment=str(account_row["account_environment"]),
            login_masked=self._mask_login(str(account_row["login"])),
            server=str(account_row["server"]),
            error_code=None,
            read_at=read_at,
        )
        view = self._without_live_state(connection=connection, trading=self._trading(user_id), user_id=user_id, now=now)
        return replace(view, account=account)

    def _last_confirmed_account(self, user_id: UUID) -> Day32Account | None:
        snapshot = self._last_confirmed_account_with_time(user_id)
        return snapshot[0] if snapshot is not None else None

    def _last_confirmed_account_with_time(self, user_id: UUID) -> tuple[Day32Account, datetime] | None:
        try:
            with self._session_factory() as session:
                row = session.execute(text("""
                    SELECT last_confirmed_currency,last_confirmed_balance,last_confirmed_equity,
                           last_confirmed_margin,last_confirmed_free_margin,last_confirmed_trade_allowed,
                           last_confirmed_account_at
                    FROM mt5_accounts
                    WHERE owner_user_id=:user_id AND status<>'revoked'
                    ORDER BY created_at DESC LIMIT 1
                """), {"user_id": user_id}).mappings().first()
        except Exception:
            logger.exception("Last-confirmed MT5 dashboard snapshot read failed")
            return None
        if row is None or row["last_confirmed_account_at"] is None:
            return None
        required = ("last_confirmed_currency","last_confirmed_balance","last_confirmed_equity","last_confirmed_margin","last_confirmed_free_margin","last_confirmed_trade_allowed")
        if any(row[key] is None for key in required):
            return None
        account = Day32Account(
            currency=str(row["last_confirmed_currency"]),
            balance=float(row["last_confirmed_balance"]),
            equity=float(row["last_confirmed_equity"]),
            margin=float(row["last_confirmed_margin"]),
            free_margin=float(row["last_confirmed_free_margin"]),
            trade_allowed=bool(row["last_confirmed_trade_allowed"]),
        )
        return account, row["last_confirmed_account_at"]

    def _persist_last_confirmed_account(self, user_id: UUID, account: Day32Account, *, read_at: datetime) -> None:
        try:
            with self._session_factory() as session:
                session.execute(text("""
                    UPDATE mt5_accounts
                    SET last_confirmed_currency=:currency,last_confirmed_balance=:balance,
                        last_confirmed_equity=:equity,last_confirmed_margin=:margin,
                        last_confirmed_free_margin=:free_margin,last_confirmed_trade_allowed=:trade_allowed,
                        last_confirmed_account_at=:read_at
                    WHERE id=(SELECT id FROM mt5_accounts WHERE owner_user_id=:user_id
                              AND status<>'revoked' ORDER BY created_at DESC LIMIT 1)
                """), {
                    "user_id": user_id,"currency": account.currency,"balance": account.balance,
                    "equity": account.equity,"margin": account.margin,"free_margin": account.free_margin,
                    "trade_allowed": account.trade_allowed,"read_at": read_at,
                })
                session.commit()
        except Exception:
            logger.exception("Last-confirmed MT5 dashboard snapshot write failed")


__all__ = ["ResilientDashboardRuntimeService"]
