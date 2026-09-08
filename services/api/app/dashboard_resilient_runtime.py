"""Persistent last-confirmed MT5 account fallback for the mobile dashboard.

This layer never invents broker state. A successful canonical dashboard read stores the
exact account values that were displayed. Dashboard requests are intentionally
stale-while-revalidate: the last confirmed view is returned immediately and one bounded
broker refresh runs in the background. This keeps app startup independent from MetaAPI
latency while preserving broker truth as the next confirmed state.
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

logger = logging.getLogger(__name__)


class ResilientDashboardRuntimeService(CanonicalDashboardRuntimeService):
    """Canonical dashboard with durable and in-memory last-confirmed snapshots."""

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002,ANN003
        super().__init__(*args, **kwargs)
        self._last_views: dict[UUID, Day32DashboardView] = {}
        self._refresh_tasks: dict[UUID, asyncio.Task[None]] = {}

    def _ensure_runtime_state(self) -> None:
        # A few focused unit tests construct this service with object.__new__ and
        # monkeypatch the base read method. Keep that compatibility while production
        # instances still initialise these fields normally through __init__.
        if not hasattr(self, "_last_views"):
            self._last_views = {}
        if not hasattr(self, "_refresh_tasks"):
            self._refresh_tasks = {}

    async def read(self, user_id: UUID) -> Day32DashboardView:
        """Return immediately from confirmed state and refresh broker state once.

        A connected account must not make first paint wait on an external broker call.
        The latest in-memory view is preferred. After a process restart, the durable
        account snapshot plus local position ledger provides the immediate fallback.
        """
        self._ensure_runtime_state()

        # Preserve the isolated fallback tests which intentionally construct the object
        # without a database session factory. Real application instances always have it.
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
            # This only happens before the first ever confirmed broker read for an
            # account. There is no honest snapshot to display yet, so one bounded live
            # read remains necessary.
            return await self._read_live_and_cache(user_id)

        self._ensure_live_refresh(user_id)
        return cached

    async def _read_live_and_cache(self, user_id: UUID) -> Day32DashboardView:
        self._ensure_runtime_state()
        view = await super().read(user_id)
        if view.account is not None:
            self._persist_last_confirmed_account(
                user_id,
                view.account,
                read_at=view.connection.read_at or datetime.now(UTC),
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

    def _ensure_live_refresh(self, user_id: UUID) -> None:
        self._ensure_runtime_state()
        current = self._refresh_tasks.get(user_id)
        if current is not None and not current.done():
            return
        self._refresh_tasks[user_id] = asyncio.create_task(
            self._refresh_live(user_id),
            name=f"dashboard-live-refresh-{user_id}",
        )

    async def _refresh_live(self, user_id: UUID) -> None:
        try:
            view = await self._read_live_and_cache(user_id)
            if view.connection.status == "connected" and view.account is not None:
                self._last_views[user_id] = view
        except asyncio.CancelledError:
            raise
        except Exception:
            # Dashboard freshness must never take down the app shell. The previously
            # confirmed view remains available and the next request will retry.
            logger.exception("Background MT5 dashboard refresh failed safely")
        finally:
            self._refresh_tasks.pop(user_id, None)

    def _durable_dashboard_snapshot(
        self,
        user_id: UUID,
        *,
        account_row,
    ) -> Day32DashboardView | None:  # noqa: ANN001
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
        view = self._without_live_state(
            connection=connection,
            trading=self._trading(user_id),
            user_id=user_id,
            now=now,
        )
        return replace(view, account=account)

    def _last_confirmed_account(self, user_id: UUID) -> Day32Account | None:
        snapshot = self._last_confirmed_account_with_time(user_id)
        return snapshot[0] if snapshot is not None else None

    def _last_confirmed_account_with_time(
        self,
        user_id: UUID,
    ) -> tuple[Day32Account, datetime] | None:
        try:
            with self._session_factory() as session:
                row = session.execute(
                    text(
                        """
                        SELECT
                            last_confirmed_currency,
                            last_confirmed_balance,
                            last_confirmed_equity,
                            last_confirmed_margin,
                            last_confirmed_free_margin,
                            last_confirmed_trade_allowed,
                            last_confirmed_account_at
                        FROM mt5_accounts
                        WHERE owner_user_id=:user_id
                          AND status<>'revoked'
                        ORDER BY created_at DESC
                        LIMIT 1
                        """
                    ),
                    {"user_id": user_id},
                ).mappings().first()
        except Exception:
            logger.exception("Last-confirmed MT5 dashboard snapshot read failed")
            return None

        if row is None or row["last_confirmed_account_at"] is None:
            return None
        required = (
            "last_confirmed_currency",
            "last_confirmed_balance",
            "last_confirmed_equity",
            "last_confirmed_margin",
            "last_confirmed_free_margin",
            "last_confirmed_trade_allowed",
        )
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

    def _persist_last_confirmed_account(
        self,
        user_id: UUID,
        account: Day32Account,
        *,
        read_at: datetime,
    ) -> None:
        try:
            with self._session_factory() as session:
                session.execute(
                    text(
                        """
                        UPDATE mt5_accounts
                        SET last_confirmed_currency=:currency,
                            last_confirmed_balance=:balance,
                            last_confirmed_equity=:equity,
                            last_confirmed_margin=:margin,
                            last_confirmed_free_margin=:free_margin,
                            last_confirmed_trade_allowed=:trade_allowed,
                            last_confirmed_account_at=:read_at
                        WHERE id=(
                            SELECT id
                            FROM mt5_accounts
                            WHERE owner_user_id=:user_id
                              AND status<>'revoked'
                            ORDER BY created_at DESC
                            LIMIT 1
                        )
                        """
                    ),
                    {
                        "user_id": user_id,
                        "currency": account.currency,
                        "balance": account.balance,
                        "equity": account.equity,
                        "margin": account.margin,
                        "free_margin": account.free_margin,
                        "trade_allowed": account.trade_allowed,
                        "read_at": read_at,
                    },
                )
                session.commit()
        except Exception:
            # Snapshot persistence is observability hardening only. Never turn a
            # successful broker dashboard read into an application failure because the
            # cache write itself had a transient database problem.
            logger.exception("Last-confirmed MT5 dashboard snapshot write failed")


__all__ = ["ResilientDashboardRuntimeService"]
