"""Day 35 admin Signal Portfolio read model.

This layer deliberately reuses Day 33's broker-backed outcome rows and canonical
_summary_metrics implementation. It never derives performance from Telegram claims and
never aggregates multiple user copies of the same signal. The Owner account is the
single reference execution ledger for provider/trader comparison.

Realised performance remains Day 33 truth. Current floating P/L is an independent,
read-only MetaAPI positions snapshot matched only to mapped Super Signals broker
position IDs. Failure of the live read never corrupts or suppresses realised metrics.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import text

from app.metaapi_gateway import MetaApiGatewayError
from app.mt5_crypto import BrokerCredentialDecryptionError
from app.performance_ledger_day33 import _d, _pct, stable_color_index
from app.performance_ledger_day33_v2 import Day33PerformanceLedgerServiceV2

PeriodKey = Literal["today", "7d", "month", "year", "all"]
SortKey = Literal["realized_pnl", "return_percent", "win_rate", "trade_count"]


@dataclass(frozen=True, slots=True)
class Day35PortfolioRow:
    dimension_type: str
    source_id: UUID
    source_label: str
    trader_stream: str | None
    source_color_index: int
    realized_cash_pnl: Decimal
    open_cash_pnl: Decimal | None
    open_cash_pnl_known: bool
    return_percent: Decimal | None
    trades_closed: int
    trades_open: int
    wins: int
    losses: int
    breakeven: int
    win_rate_percent: Decimal | None
    net_pips: Decimal | None
    rank: int = 0


@dataclass(frozen=True, slots=True)
class Day35PortfolioView:
    period_key: PeriodKey
    period_label: str
    period_start: datetime
    period_end: datetime
    reference_user_id: UUID
    rows: tuple[Day35PortfolioRow, ...]
    performance_basis: str = "day33_broker_deal_ledger"
    provider_identity_visible: bool = True
    broker_trade_action_created: bool = False
    open_cash_pnl_live: bool = False
    open_cash_pnl_as_of: datetime | None = None
    open_cash_pnl_error_code: str | None = None


@dataclass(frozen=True, slots=True)
class _OpenRequirement:
    source_id: UUID
    trader_stream: str | None
    status: str
    broker_position_id: str | None


class Day35AdminPortfolioService:
    """Read-only source/trader comparison over the canonical Day 33 ledger."""

    def __init__(self, ledger: Day33PerformanceLedgerServiceV2) -> None:
        self._ledger = ledger
        self._session_factory = ledger._session_factory

    def read(
        self,
        period: PeriodKey,
        *,
        sort_by: SortKey = "realized_pnl",
        now: datetime | None = None,
    ) -> Day35PortfolioView:
        point = self._utc(now or datetime.now(UTC))
        start, label = self._period_start(period, point)
        reference_user_id = self._owner_reference_user_id()
        outcome_rows = self._outcomes(reference_user_id, start=start, end=point)

        dimensions: dict[tuple[str, UUID, str | None], list[Any]] = {}
        labels: dict[UUID, str] = {}
        for row in outcome_rows:
            source_id = row["source_id"]
            if not isinstance(source_id, UUID):
                continue
            source_label = str(row["source_label"] or "Unknown source")
            labels[source_id] = source_label
            dimensions.setdefault(("source", source_id, None), []).append(row)
            trader_stream = str(row["trader_stream"] or "").strip() or None
            if trader_stream is not None:
                dimensions.setdefault(("trader", source_id, trader_stream), []).append(row)

        result: list[Day35PortfolioRow] = []
        for (dimension_type, source_id, trader_stream), rows in dimensions.items():
            metrics = self._ledger._summary_metrics(
                reference_user_id,
                rows,
                start,
                point,
            )
            wins = int(metrics["wins"])
            losses = int(metrics["losses"])
            decided = wins + losses
            win_rate = (
                _pct(Decimal(wins) / Decimal(decided) * Decimal("100"))
                if decided
                else None
            )
            result.append(
                Day35PortfolioRow(
                    dimension_type=dimension_type,
                    source_id=source_id,
                    source_label=labels[source_id],
                    trader_stream=trader_stream,
                    source_color_index=stable_color_index(source_id, trader_stream),
                    realized_cash_pnl=_d(metrics["cash_pnl"]),
                    open_cash_pnl=None,
                    open_cash_pnl_known=False,
                    return_percent=(
                        _d(metrics["return_percent"])
                        if metrics["return_percent"] is not None
                        else None
                    ),
                    trades_closed=int(metrics["total_trades"]),
                    trades_open=int(metrics["open_trades"]),
                    wins=wins,
                    losses=losses,
                    breakeven=int(metrics["breakeven"]),
                    win_rate_percent=win_rate,
                    net_pips=(
                        _d(metrics["net_pips"])
                        if metrics["net_pips"] is not None
                        else None
                    ),
                )
            )

        ordered = sorted(result, key=lambda row: self._sort_value(row, sort_by), reverse=True)
        ranked = tuple(replace(row, rank=index) for index, row in enumerate(ordered, start=1))
        return Day35PortfolioView(
            period_key=period,
            period_label=label,
            period_start=start,
            period_end=point,
            reference_user_id=reference_user_id,
            rows=ranked,
        )

    async def read_with_live_open_pnl(
        self,
        period: PeriodKey,
        *,
        sort_by: SortKey = "realized_pnl",
        now: datetime | None = None,
    ) -> Day35PortfolioView:
        """Add a broker-live floating P/L snapshot without changing Day 33 truth."""

        base = self.read(period, sort_by=sort_by, now=now)
        requirements = self._open_requirements(
            base.reference_user_id,
            start=base.period_start,
            end=base.period_end,
        )

        # Closed-only dimensions have known zero floating P/L and do not require a
        # network call. A pending-only dimension also has no floating position P/L.
        if not any(item.status == "open" for item in requirements):
            return replace(
                base,
                rows=tuple(
                    replace(row, open_cash_pnl=Decimal("0"), open_cash_pnl_known=True)
                    for row in base.rows
                ),
                open_cash_pnl_live=True,
                open_cash_pnl_as_of=base.period_end,
            )

        try:
            broker_profit = await self._read_live_broker_position_profit(base.reference_user_id)
        except BrokerCredentialDecryptionError:
            return self._with_live_failure(base, requirements, "broker_credential_decryption_failed")
        except MetaApiGatewayError as exc:
            return self._with_live_failure(base, requirements, exc.code)
        except RuntimeError as exc:
            return self._with_live_failure(base, requirements, str(exc))

        enriched: list[Day35PortfolioRow] = []
        for row in base.rows:
            relevant = self._requirements_for_row(row, requirements)
            open_items = [item for item in relevant if item.status == "open"]
            if not open_items:
                enriched.append(replace(row, open_cash_pnl=Decimal("0"), open_cash_pnl_known=True))
                continue

            ids = [item.broker_position_id for item in open_items]
            if any(not value for value in ids):
                enriched.append(replace(row, open_cash_pnl=None, open_cash_pnl_known=False))
                continue
            profits = [broker_profit.get(str(value)) for value in ids if value]
            if len(profits) != len(ids) or any(value is None for value in profits):
                # The broker may have settled a mapped position between ledger and live
                # reads. Settlement/reconciliation owns that transition; the portfolio
                # must not invent zero while those two truths converge.
                enriched.append(replace(row, open_cash_pnl=None, open_cash_pnl_known=False))
                continue
            value = sum((item for item in profits if item is not None), Decimal("0"))
            enriched.append(replace(row, open_cash_pnl=value, open_cash_pnl_known=True))

        return replace(
            base,
            rows=tuple(enriched),
            open_cash_pnl_live=True,
            open_cash_pnl_as_of=datetime.now(UTC),
            open_cash_pnl_error_code=None,
        )

    def _with_live_failure(
        self,
        base: Day35PortfolioView,
        requirements: list[_OpenRequirement],
        code: str,
    ) -> Day35PortfolioView:
        rows: list[Day35PortfolioRow] = []
        for row in base.rows:
            relevant = self._requirements_for_row(row, requirements)
            if any(item.status == "open" for item in relevant):
                rows.append(replace(row, open_cash_pnl=None, open_cash_pnl_known=False))
            else:
                rows.append(replace(row, open_cash_pnl=Decimal("0"), open_cash_pnl_known=True))
        return replace(
            base,
            rows=tuple(rows),
            open_cash_pnl_live=False,
            open_cash_pnl_as_of=None,
            open_cash_pnl_error_code=code,
        )

    @staticmethod
    def _requirements_for_row(
        row: Day35PortfolioRow,
        requirements: list[_OpenRequirement],
    ) -> list[_OpenRequirement]:
        return [
            item
            for item in requirements
            if item.source_id == row.source_id
            and (
                row.dimension_type == "source"
                or item.trader_stream == row.trader_stream
            )
        ]

    async def _read_live_broker_position_profit(self, user_id: UUID) -> dict[str, Decimal]:
        account = self._reference_account(user_id)
        if account is None:
            raise RuntimeError("mt5_account_not_configured")
        if str(account["status"]) != "connected":
            raise RuntimeError("mt5_account_not_connected")

        token = self._ledger._cipher.decrypt(bytes(account["metaapi_token_ciphertext"]))
        account_id = str(account["metaapi_account_id"])
        region = await self._ledger._gateway.resolve_account_region(
            token=token,
            account_id=account_id,
        )
        payloads = await self._ledger._gateway.read_positions(
            token=token,
            account_id=account_id,
            region=region,
        )

        result: dict[str, Decimal] = {}
        for item in payloads:
            broker_id = str(item.get("id") or "").strip()
            if not broker_id:
                continue
            profit = self._decimal_or_none(item.get("profit"))
            if profit is not None:
                result[broker_id] = profit
        return result

    def _reference_account(self, user_id: UUID) -> Any | None:
        with self._session_factory() as session:
            return session.execute(
                text(
                    """
                    SELECT metaapi_account_id, metaapi_token_ciphertext, status
                    FROM mt5_accounts
                    WHERE owner_user_id=:user_id
                      AND status!='revoked'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {"user_id": user_id},
            ).mappings().first()

    def _open_requirements(
        self,
        user_id: UUID,
        *,
        start: datetime,
        end: datetime,
    ) -> list[_OpenRequirement]:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT o.source_id, o.trader_stream, o.status, p.broker_position_id
                    FROM performance_trade_outcomes o
                    JOIN positions p ON p.id=o.position_id
                    WHERE o.user_id=:user_id
                      AND o.status IN ('open','pending')
                      AND o.opened_at IS NOT NULL
                      AND o.opened_at>=:period_start
                      AND o.opened_at<=:period_end
                    ORDER BY o.opened_at,o.position_id
                    """
                ),
                {
                    "user_id": user_id,
                    "period_start": start,
                    "period_end": end,
                },
            ).mappings().all()
        result: list[_OpenRequirement] = []
        for row in rows:
            if not isinstance(row["source_id"], UUID):
                continue
            result.append(
                _OpenRequirement(
                    source_id=row["source_id"],
                    trader_stream=(str(row["trader_stream"]) if row["trader_stream"] else None),
                    status=str(row["status"]),
                    broker_position_id=(
                        str(row["broker_position_id"])
                        if row["broker_position_id"] is not None
                        else None
                    ),
                )
            )
        return result

    @staticmethod
    def _period_start(period: PeriodKey, now: datetime) -> tuple[datetime, str]:
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if period == "today":
            return today, "Today"
        if period == "7d":
            return now - timedelta(days=7), "7 days"
        if period == "month":
            return today.replace(day=1), "Month"
        if period == "year":
            return today.replace(month=1, day=1), "Year"
        if period == "all":
            return datetime(1970, 1, 1, tzinfo=UTC), "All time"
        raise ValueError("Unsupported Day 35 portfolio period")

    @staticmethod
    def _sort_value(row: Day35PortfolioRow, sort_by: SortKey) -> tuple[Decimal, Decimal]:
        if sort_by == "return_percent":
            return (row.return_percent or Decimal("-999999999"), row.realized_cash_pnl)
        if sort_by == "win_rate":
            return (row.win_rate_percent or Decimal("-1"), row.realized_cash_pnl)
        if sort_by == "trade_count":
            return (Decimal(row.trades_closed + row.trades_open), row.realized_cash_pnl)
        return (row.realized_cash_pnl, row.return_percent or Decimal("-999999999"))

    def _owner_reference_user_id(self) -> UUID:
        with self._session_factory() as session:
            value = session.execute(
                text(
                    """
                    SELECT u.id
                    FROM users u
                    JOIN user_roles ur ON ur.user_id=u.id
                    JOIN roles r ON r.id=ur.role_id
                    WHERE r.name='owner'
                      AND u.status NOT IN ('revoked','suspended')
                    ORDER BY u.created_at,u.id
                    LIMIT 1
                    """
                )
            ).scalar_one_or_none()
        if not isinstance(value, UUID):
            raise RuntimeError("day35_owner_reference_user_missing")
        return value

    def _outcomes(
        self,
        user_id: UUID,
        *,
        start: datetime,
        end: datetime,
    ) -> list[Any]:
        with self._session_factory() as session:
            return list(
                session.execute(
                    text(
                        """
                        SELECT
                            o.*,
                            COALESCE(src.source_alias,src.chat_title,'Unknown source') AS source_label
                        FROM performance_trade_outcomes o
                        LEFT JOIN sources src ON src.id=o.source_id
                        WHERE o.user_id=:user_id
                          AND (
                              (o.closed_at IS NOT NULL AND o.closed_at>=:period_start AND o.closed_at<=:period_end)
                              OR (
                                  o.status IN ('open','pending')
                                  AND o.opened_at IS NOT NULL
                                  AND o.opened_at>=:period_start
                                  AND o.opened_at<=:period_end
                              )
                          )
                        ORDER BY COALESCE(o.closed_at,o.opened_at),o.position_id
                        """
                    ),
                    {
                        "user_id": user_id,
                        "period_start": start,
                        "period_end": end,
                    },
                ).mappings().all()
            )

    @staticmethod
    def _decimal_or_none(value: object | None) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return None

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


__all__ = [
    "Day35AdminPortfolioService",
    "Day35PortfolioRow",
    "Day35PortfolioView",
    "PeriodKey",
    "SortKey",
]
