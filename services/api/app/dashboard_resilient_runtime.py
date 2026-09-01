"""Persistent last-confirmed MT5 account fallback for the mobile dashboard.

This layer never invents broker state. A successful canonical dashboard read stores the
exact account values that were displayed. If a later MetaAPI read fails transiently, the
same account can be returned with connection.status='connection_error'; the frontend
already labels that state as last confirmed rather than live.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text

from app.dashboard_day32 import Day32Account, Day32DashboardView
from app.dashboard_runtime import CanonicalDashboardRuntimeService

logger = logging.getLogger(__name__)


class ResilientDashboardRuntimeService(CanonicalDashboardRuntimeService):
    """Canonical dashboard with a durable cross-device account-value fallback."""

    async def read(self, user_id: UUID) -> Day32DashboardView:
        view = await super().read(user_id)
        if view.account is not None:
            self._persist_last_confirmed_account(
                user_id,
                view.account,
                read_at=view.connection.read_at or datetime.now(UTC),
            )
            return view

        if not view.connection.configured or view.connection.status != "connection_error":
            return view

        cached = self._last_confirmed_account(user_id)
        if cached is None:
            return view
        return replace(view, account=cached)

    def _last_confirmed_account(self, user_id: UUID) -> Day32Account | None:
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
        return Day32Account(
            currency=str(row["last_confirmed_currency"]),
            balance=float(row["last_confirmed_balance"]),
            equity=float(row["last_confirmed_equity"]),
            margin=float(row["last_confirmed_margin"]),
            free_margin=float(row["last_confirmed_free_margin"]),
            trade_allowed=bool(row["last_confirmed_trade_allowed"]),
        )

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
