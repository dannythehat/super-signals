"""Live XAU/USD quote plus a privacy-safe public performance feed.

The Gold quote remains isolated from MetaAPI and the user's broker account. The public
performance feed is read-only and exposes only the Owner reference ledger needed by the
Smart Signals public website: daily realised P/L plus provider-hidden trade outcomes.
Audited reporting overrides are applied to the public cash ledger without changing the
immutable broker evidence, and revoked providers remain outside user-facing performance.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from math import isfinite
from time import monotonic
from typing import Annotated, Any
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import text

from app.access_control import get_current_identity
from app.performance_ledger_day33_v2 import Day33PerformanceLedgerServiceV2
from app.reporting_overrides import (
    BROKER_DEAL_NOT_OVERRIDDEN_SQL,
    override_cash_by_day,
)
from app.running_daily_balance import account_value_days
from app.trading_accounting import CanonicalTradingAccountingService

router = APIRouter(prefix="/dashboard", tags=["dashboard-public-data"])
Identity = Annotated[dict[str, Any], Depends(get_current_identity)]

_SYMBOL = "XAUUSD"
_PRIMARY_URL = "https://biquote.io/api/XAUUSD?allowStale=false"
_FALLBACK_URL = "https://api.gold-api.com/price/XAU"
_LIVE_CACHE_SECONDS = 0.75
_RETRY_CACHE_SECONDS = 2.0
_PUBLIC_TIMEZONE = "Europe/Sofia"
_PUBLIC_LIVE_START = date(2026, 8, 31)
_PUBLIC_TRADE_DETAIL_START = date(2026, 9, 3)
_PUBLIC_STARTING_BALANCE = 1517.23
_HISTORICAL_STARTING_BALANCE = 1517.23

# Owner instruction, 23 Sep 2026: TRADE GLOBAL (switched to live trading that day against
# the owner's standing decision) and the "Scalping 📈" channel are left out of the public
# trade log from that day on. The owner reversed their cash impact for the day on the
# paper account, so the 21:00-equity daily P/L already reflects that; this only keeps the
# trade log free of their individual rows. Matched by chat id so a rename cannot bring
# them back.
_PUBLIC_TRADE_LOG_EXCLUDED_CHAT_IDS: tuple[int, ...] = (
    -1003925988158,  # TRADE GLOBAL
    -1004469449988,  # Scalping 📈
)
_PUBLIC_TRADE_LOG_EXCLUDED_FROM = date(2026, 9, 23)


class GoldQuoteResponse(BaseModel):
    symbol: str = _SYMBOL
    price: float | None
    bid: float | None
    ask: float | None
    quote_time: datetime | None
    read_at: datetime
    available: bool
    stale: bool
    source: str


class PublicDailyPnlResponse(BaseModel):
    day: date
    pnl: float
    opening_balance: float | None = None
    closing_balance: float | None = None
    running: bool = False


class PublicTradeResponse(BaseModel):
    day: date
    signal_id: UUID
    symbol: str
    side: str
    status: str
    status_label: str
    opened_at: datetime | None
    closed_at: datetime | None
    position_count: int
    open_positions: int
    pending_positions: int
    closed_positions: int
    cash_pnl: float | None
    net_pips: float | None
    close_reason: str | None


class PublicPerformanceResponse(BaseModel):
    timezone: str = _PUBLIC_TIMEZONE
    live_start_date: date = _PUBLIC_LIVE_START
    trade_detail_start_date: date = _PUBLIC_TRADE_DETAIL_START
    live_starting_balance: float = _PUBLIC_STARTING_BALANCE
    current_recorded_balance: float
    total_recorded_pnl: float
    return_percent: float
    current_account_value: float | None = None
    current_mt5_balance: float | None = None
    account_value_updated_at: datetime | None = None
    daily: tuple[PublicDailyPnlResponse, ...]
    trades: tuple[PublicTradeResponse, ...]
    updated_at: datetime

class PublicTradeDayResponse(BaseModel):
    day: date
    trades: tuple[PublicTradeResponse, ...]
    updated_at: datetime



def _positive_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if isfinite(parsed) and parsed > 0 else None


def _quote_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _biquote_quote(payload: dict[str, object], *, now: datetime) -> GoldQuoteResponse:
    bid = _positive_float(payload.get("bid"))
    ask = _positive_float(payload.get("ask"))
    price = _positive_float(payload.get("mid"))
    if price is None and bid is not None and ask is not None:
        price = round((bid + ask) / 2.0, 5)

    timestamp = _quote_time(payload.get("timestamp") or payload.get("lastQuoteAt"))
    market_state = str(payload.get("marketState") or "").strip().lower()
    upstream_stale = bool(payload.get("stale"))
    quote_age = _positive_float(payload.get("quoteAgeSeconds"))
    stale = upstream_stale or market_state == "closed" or (quote_age is not None and quote_age > 5)

    return GoldQuoteResponse(
        price=price,
        bid=bid,
        ask=ask,
        quote_time=timestamp,
        read_at=now,
        available=price is not None,
        stale=stale,
        source="biquote live MT5",
    )


def _gold_api_quote(payload: dict[str, object], *, now: datetime) -> GoldQuoteResponse:
    price = _positive_float(payload.get("price"))
    return GoldQuoteResponse(
        price=price,
        bid=_positive_float(payload.get("bid")),
        ask=_positive_float(payload.get("ask")),
        quote_time=_quote_time(payload.get("updatedAt") or payload.get("updated_at")),
        read_at=now,
        available=price is not None,
        stale=True,
        source="Gold API fallback",
    )


def _unavailable(now: datetime) -> GoldQuoteResponse:
    return GoldQuoteResponse(
        price=None,
        bid=None,
        ask=None,
        quote_time=None,
        read_at=now,
        available=False,
        stale=True,
        source="Free gold feeds",
    )


def _cache(request: Request) -> dict[str, tuple[float, GoldQuoteResponse]]:
    existing = getattr(request.app.state, "gold_quote_cache", None)
    if isinstance(existing, dict):
        return existing
    created: dict[str, tuple[float, GoldQuoteResponse]] = {}
    request.app.state.gold_quote_cache = created
    return created


def _store(cache: dict[str, tuple[float, GoldQuoteResponse]], quote: GoldQuoteResponse) -> GoldQuoteResponse:
    ttl = _LIVE_CACHE_SECONDS if quote.available and not quote.stale else _RETRY_CACHE_SECONDS
    cache[_SYMBOL] = (monotonic() + ttl, quote)
    return quote


async def _read_json(url: str) -> dict[str, object]:
    timeout = httpx.Timeout(3.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(
            url,
            headers={
                "Accept": "application/json",
                "Cache-Control": "no-cache",
                "User-Agent": "SuperSignals/1.0",
            },
        )
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("invalid_gold_feed_response")
    return payload


def _performance_service(request: Request) -> Day33PerformanceLedgerServiceV2:
    service = getattr(request.app.state, "day33_performance_service", None)
    if not isinstance(service, Day33PerformanceLedgerServiceV2):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "public_performance_unavailable",
                "message": "Public performance data is temporarily unavailable.",
            },
        )
    return service


def _owner_reference_user_id(service: Day33PerformanceLedgerServiceV2) -> UUID:
    with service._session_factory() as session:
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
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "public_performance_owner_missing",
                "message": "Public performance data is temporarily unavailable.",
            },
        )
    return value


def _canonical_displayed_balance(service: Day33PerformanceLedgerServiceV2, user_id: UUID) -> Decimal:
    """Newest full Vantage account value (equity), never a reconstruction."""
    accounting = CanonicalTradingAccountingService(service._session_factory)
    with service._session_factory() as session:
        row = session.execute(
            text(
                """
                SELECT pas.equity
                FROM performance_account_snapshots pas
                JOIN mt5_accounts a ON a.id=pas.mt5_account_id
                WHERE a.owner_user_id=:user_id
                  AND a.status<>'revoked'
                  AND pas.equity IS NOT NULL
                ORDER BY pas.captured_at DESC
                LIMIT 1
                """
            ),
            {"user_id": user_id},
        ).mappings().first()
    account_value = row["equity"] if row is not None else Decimal("0")
    return accounting.displayed_balance(
        user_id,
        broker_account_value=account_value,
    )


def _latest_owner_account_value(
    service: Day33PerformanceLedgerServiceV2,
    user_id: UUID,
) -> tuple[float | None, float | None, datetime | None]:
    """Return the latest MetaAPI-backed owner account snapshot.

    The public headline uses full MT5/Vantage equity (closed balance plus floating P/L).
    The raw closed balance is returned separately for audit/debugging.
    """
    with service._session_factory() as session:
        row = session.execute(
            text(
                """
                SELECT pas.balance,pas.equity,pas.captured_at
                FROM performance_account_snapshots pas
                JOIN mt5_accounts a ON a.id=pas.mt5_account_id
                WHERE a.owner_user_id=:user_id
                  AND a.status<>'revoked'
                ORDER BY pas.captured_at DESC
                LIMIT 1
                """
            ),
            {"user_id": user_id},
        ).mappings().first()
    if row is None:
        return None, None, None
    equity = float(row["equity"]) if row["equity"] is not None else None
    balance = float(row["balance"]) if row["balance"] is not None else None
    return equity, balance, row["captured_at"]


def _public_daily(service: Day33PerformanceLedgerServiceV2, user_id: UUID) -> tuple[PublicDailyPnlResponse, ...]:
    now = datetime.now(UTC)
    public_zone = ZoneInfo(_PUBLIC_TIMEZONE)
    public_start = datetime(
        _PUBLIC_LIVE_START.year,
        _PUBLIC_LIVE_START.month,
        _PUBLIC_LIVE_START.day,
        tzinfo=public_zone,
    ).astimezone(UTC)
    with service._session_factory() as session:
        rows = session.execute(
            text(
                f"""
                SELECT
                    timezone(:timezone_name, bd.occurred_at)::date AS local_day,
                    COALESCE(SUM(
                        COALESCE(bd.profit,0)
                        + COALESCE(bd.commission,0)
                        + COALESCE(bd.swap,0)
                    ),0) AS pnl
                FROM broker_deals bd
                JOIN sources src ON src.id=bd.source_id
                WHERE bd.user_id=:user_id
                  AND src.status<>'revoked'
                  AND bd.entry_type='DEAL_ENTRY_OUT'
                  AND (bd.signal_id IS NOT NULL OR bd.broker_client_id LIKE 'SS_%')
                  AND bd.occurred_at>=:start_at
                  AND bd.occurred_at<:end_at
                  AND {BROKER_DEAL_NOT_OVERRIDDEN_SQL}
                GROUP BY 1
                ORDER BY 1
                """
            ),
            {
                "user_id": user_id,
                "timezone_name": _PUBLIC_TIMEZONE,
                "start_at": public_start,
                "end_at": now,
            },
        ).mappings().all()
        reviewed_rows = session.execute(
            text(
                """
                SELECT
                    timezone(:timezone_name, pto.closed_at)::date AS local_day,
                    COALESCE(SUM(pto.cash_pnl),0) AS pnl
                FROM performance_trade_outcomes pto
                WHERE pto.user_id=:user_id
                  AND pto.closed_at IS NOT NULL
                  AND pto.closed_at>=:start_at
                  AND pto.closed_at<:end_at
                  AND pto.broker_deal_count=0
                  AND pto.close_reason LIKE 'reviewed_provider_%'
                GROUP BY 1
                ORDER BY 1
                """
            ),
            {
                "user_id": user_id,
                "timezone_name": _PUBLIC_TIMEZONE,
                "start_at": public_start,
                "end_at": now,
            },
        ).mappings().all()
        reviewed_by_day = override_cash_by_day(
            session,
            user_id,
            start=public_start,
            end=now,
        )
        account_days = account_value_days(
            session,
            user_id,
            now=now,
        )

    values = {
        row["local_day"]: Decimal(str(row["pnl"] or 0))
        for row in rows
    }
    for row in reviewed_rows:
        reporting_day = row["local_day"]
        values[reporting_day] = values.get(reporting_day, Decimal("0")) + Decimal(str(row["pnl"] or 0))
    for reporting_day, reviewed_cash in reviewed_by_day.items():
        values[reporting_day] = values.get(reporting_day, Decimal("0")) + reviewed_cash

    account_by_day = {item.day: item for item in account_days}
    all_days = sorted(set(values) | set(account_by_day))
    local_now = now.astimezone(public_zone)
    current_day = (
        local_now.date() + timedelta(days=1)
        if local_now.hour >= 21
        else local_now.date()
    )
    result: list[PublicDailyPnlResponse] = []
    for day in all_days:
        account_day = account_by_day.get(day)
        if account_day is not None:
            result.append(
                PublicDailyPnlResponse(
                    day=day,
                    pnl=round(float(account_day.pnl), 2),
                    opening_balance=round(float(account_day.opening_value), 2),
                    closing_balance=round(float(account_day.closing_value), 2),
                    running=day == current_day,
                )
            )
        else:
            result.append(
                PublicDailyPnlResponse(
                    day=day,
                    pnl=round(float(values.get(day, Decimal("0"))), 2),
                )
            )
    return tuple(result)


def _excluded_trade_log_signal_ids(
    service: Day33PerformanceLedgerServiceV2,
    user_id: UUID,
) -> frozenset[UUID]:
    chat_ids = ",".join(str(int(chat_id)) for chat_id in _PUBLIC_TRADE_LOG_EXCLUDED_CHAT_IDS)
    with service._session_factory() as session:
        rows = session.execute(
            text(
                f"""
                SELECT DISTINCT p.signal_id
                FROM positions p
                JOIN signals s ON s.id=p.signal_id
                JOIN sources src ON src.id=s.source_id
                WHERE p.user_id=:user_id
                  AND src.chat_id IN ({chat_ids})
                """
            ),
            {"user_id": user_id},
        ).scalars().all()
    return frozenset(rows)


def _public_trades(service: Day33PerformanceLedgerServiceV2, user_id: UUID) -> tuple[PublicTradeResponse, ...]:
    zone = ZoneInfo(_PUBLIC_TIMEZONE)
    excluded = _excluded_trade_log_signal_ids(service, user_id)
    timeline = service.read_timeline(
        user_id,
        viewer_role="user",
        limit=250,
    )
    trades: list[PublicTradeResponse] = []
    for item in timeline.trades:
        if int(item.position_count or 0) <= 0:
            continue
        event_time = item.opened_at or item.closed_at
        if event_time is None:
            continue
        aware = event_time if event_time.tzinfo is not None else event_time.replace(tzinfo=UTC)
        local_event = aware.astimezone(zone)
        local_day = (
            local_event.date() + timedelta(days=1)
            if local_event.hour >= 21
            else local_event.date()
        )
        if local_day < _PUBLIC_TRADE_DETAIL_START:
            continue
        if local_day >= _PUBLIC_TRADE_LOG_EXCLUDED_FROM and item.signal_id in excluded:
            continue
        trades.append(
            PublicTradeResponse(
                day=local_day,
                signal_id=item.signal_id,
                symbol=item.symbol,
                side=item.side,
                status=item.status,
                status_label=item.status_label,
                opened_at=item.opened_at,
                closed_at=item.closed_at,
                position_count=item.position_count,
                open_positions=item.open_positions,
                pending_positions=item.pending_positions,
                closed_positions=item.closed_positions,
                cash_pnl=(float(item.cash_pnl) if item.cash_pnl is not None else None),
                net_pips=(float(item.net_pips) if item.net_pips is not None else None),
                close_reason=item.close_reason,
            )
        )
    return tuple(trades)



def _public_trades_for_day(
    service: Day33PerformanceLedgerServiceV2,
    user_id: UUID,
    reporting_day: date,
) -> tuple[PublicTradeResponse, ...]:
    """Fast provider-hidden trade evidence for one 21:00-Sofia reporting day."""
    zone = ZoneInfo(_PUBLIC_TIMEZONE)
    start_at = datetime.combine(
        reporting_day - timedelta(days=1),
        time(21, 0),
        tzinfo=zone,
    ).astimezone(UTC)
    end_at = datetime.combine(
        reporting_day,
        time(21, 0),
        tzinfo=zone,
    ).astimezone(UTC)

    exclusion_sql = ""
    params: dict[str, object] = {
        "user_id": user_id,
        "start_at": start_at,
        "end_at": end_at,
    }
    if reporting_day >= _PUBLIC_TRADE_LOG_EXCLUDED_FROM:
        exclusion_sql = """
          AND COALESCE(src.chat_id,0) NOT IN (:excluded_chat_1,:excluded_chat_2)
        """
        params["excluded_chat_1"] = _PUBLIC_TRADE_LOG_EXCLUDED_CHAT_IDS[0]
        params["excluded_chat_2"] = _PUBLIC_TRADE_LOG_EXCLUDED_CHAT_IDS[1]

    with service._session_factory() as session:
        rows = session.execute(
            text(
                f"""
                SELECT
                    s.id AS signal_id,
                    s.symbol,
                    s.side,
                    s.source_id,
                    COALESCE(src.source_alias,src.chat_title,'Unknown source') AS source_label,
                    MIN(o.opened_at) AS opened_at,
                    MAX(o.closed_at) AS closed_at,
                    COUNT(o.position_id)::int AS position_count,
                    COUNT(*) FILTER (WHERE o.status='open')::int AS open_positions,
                    COUNT(*) FILTER (WHERE o.status='pending')::int AS pending_positions,
                    COUNT(*) FILTER (
                        WHERE o.status IN ('won','lost','breakeven','closed_unknown')
                    )::int AS closed_positions,
                    SUM(o.cash_pnl) FILTER (WHERE o.cash_pnl IS NOT NULL) AS cash_pnl,
                    CASE
                        WHEN COUNT(DISTINCT o.symbol) FILTER (WHERE o.net_pips IS NOT NULL)<=1
                         AND COUNT(*) FILTER (
                             WHERE o.status IN ('won','lost','breakeven')
                               AND o.net_pips IS NULL
                         )=0
                        THEN SUM(o.net_pips)
                        ELSE NULL
                    END AS net_pips,
                    SUM(o.model_500_pnl) FILTER (
                        WHERE o.model_500_pnl IS NOT NULL
                    ) AS model_500_pnl,
                    MAX(o.trader_stream) FILTER (
                        WHERE o.trader_stream IS NOT NULL
                    ) AS trader_stream,
                    MAX(o.close_reason) FILTER (
                        WHERE o.close_reason IS NOT NULL
                    ) AS close_reason,
                    BOOL_OR(o.status='won') AS has_win,
                    BOOL_OR(o.status='lost') AS has_loss,
                    BOOL_OR(o.status='breakeven') AS has_breakeven,
                    BOOL_OR(o.status='closed_unknown') AS has_unknown
                FROM performance_trade_outcomes o
                JOIN signals s ON s.id=o.signal_id
                LEFT JOIN sources src ON src.id=s.source_id
                WHERE o.user_id=:user_id
                  AND COALESCE(src.status,'')<>'revoked'
                  {exclusion_sql}
                GROUP BY
                    s.id,s.symbol,s.side,s.source_id,src.source_alias,src.chat_title
                HAVING COALESCE(
                    MIN(o.opened_at),
                    MAX(o.closed_at),
                    MAX(o.derived_at)
                )>=:start_at
                   AND COALESCE(
                    MIN(o.opened_at),
                    MAX(o.closed_at),
                    MAX(o.derived_at)
                )<:end_at
                ORDER BY COALESCE(
                    MIN(o.opened_at),
                    MAX(o.closed_at),
                    MAX(o.derived_at)
                ) ASC,
                s.id
                """
            ),
            params,
        ).mappings().all()

    trades: list[PublicTradeResponse] = []
    for row in rows:
        item = service._timeline_trade(row, provider_visible=False)
        if int(item.position_count or 0) <= 0:
            continue
        trades.append(
            PublicTradeResponse(
                day=reporting_day,
                signal_id=item.signal_id,
                symbol=item.symbol,
                side=item.side,
                status=item.status,
                status_label=item.status_label,
                opened_at=item.opened_at,
                closed_at=item.closed_at,
                position_count=item.position_count,
                open_positions=item.open_positions,
                pending_positions=item.pending_positions,
                closed_positions=item.closed_positions,
                cash_pnl=(float(item.cash_pnl) if item.cash_pnl is not None else None),
                net_pips=(float(item.net_pips) if item.net_pips is not None else None),
                close_reason=item.close_reason,
            )
        )
    return tuple(trades)


@router.get("/gold-quote", response_model=GoldQuoteResponse)
async def account_gold_quote(
    request: Request,
    response: Response,
    _identity: Identity,
) -> GoldQuoteResponse:
    now = datetime.now(UTC)
    cache = _cache(request)
    cached = cache.get(_SYMBOL)
    if cached is not None and monotonic() < cached[0]:
        response.headers["Cache-Control"] = "no-store"
        return cached[1]

    previous = cached[1] if cached is not None else None
    quote: GoldQuoteResponse | None = None

    try:
        candidate = _biquote_quote(await _read_json(_PRIMARY_URL), now=now)
        if candidate.available:
            quote = candidate
    except (httpx.HTTPError, ValueError, TypeError):
        quote = None

    if quote is None:
        try:
            candidate = _gold_api_quote(await _read_json(_FALLBACK_URL), now=now)
            if candidate.available:
                quote = candidate
        except (httpx.HTTPError, ValueError, TypeError):
            quote = None

    if quote is None:
        if previous is not None and previous.price is not None:
            quote = previous.model_copy(update={"read_at": now, "stale": True})
        else:
            quote = _unavailable(now)

    response.headers["Cache-Control"] = "no-store"
    return _store(cache, quote)


@router.get("/public-performance-calendar", response_model=PublicPerformanceResponse)
def public_performance_calendar(
    request: Request,
    response: Response,
) -> PublicPerformanceResponse:
    """Fast 21:00-Sofia equity feed for the public calendar.

    This endpoint intentionally avoids the heavy timeline/trade-history query so a slow
    trade-detail read can never freeze the live calendar. Historical rows remain in the
    website's published JSON; this feed supplies the live Vantage account-value days.
    """
    cached = getattr(request.app.state, "public_performance_calendar_cache", None)
    if (
        isinstance(cached, tuple)
        and len(cached) == 2
        and monotonic() < cached[0]
        and isinstance(cached[1], PublicPerformanceResponse)
    ):
        response.headers["Cache-Control"] = "public, max-age=2, stale-while-revalidate=30"
        return cached[1]

    service = _performance_service(request)
    user_id = _owner_reference_user_id(service)
    now = datetime.now(UTC)
    with service._session_factory() as session:
        account_days = account_value_days(session, user_id, now=now)

    local_now = now.astimezone(ZoneInfo(_PUBLIC_TIMEZONE))
    current_day = (
        local_now.date() + timedelta(days=1)
        if local_now.hour >= 21
        else local_now.date()
    )
    daily = tuple(
        PublicDailyPnlResponse(
            day=item.day,
            pnl=round(float(item.pnl), 2),
            opening_balance=round(float(item.opening_value), 2),
            closing_balance=round(float(item.closing_value), 2),
            running=item.day == current_day,
        )
        for item in account_days
    )

    account_value, mt5_balance, account_value_updated_at = _latest_owner_account_value(
        service,
        user_id,
    )
    if account_value is not None:
        current = round(account_value, 2)
    elif daily:
        current = round(float(daily[-1].closing_balance or _HISTORICAL_STARTING_BALANCE), 2)
    else:
        current = _HISTORICAL_STARTING_BALANCE

    total = round(current - _HISTORICAL_STARTING_BALANCE, 2)
    return_percent = round(total / _HISTORICAL_STARTING_BALANCE * 100, 2)
    result = PublicPerformanceResponse(
        current_recorded_balance=current,
        total_recorded_pnl=total,
        return_percent=return_percent,
        current_account_value=(
            round(account_value, 2) if account_value is not None else None
        ),
        current_mt5_balance=(
            round(mt5_balance, 2) if mt5_balance is not None else None
        ),
        account_value_updated_at=account_value_updated_at,
        daily=daily,
        trades=(),
        updated_at=now,
    )
    request.app.state.public_performance_calendar_cache = (
        monotonic() + 3.0,
        result,
    )
    response.headers["Cache-Control"] = "public, max-age=2, stale-while-revalidate=30"
    return result


@router.get(
    "/public-performance-trades/{reporting_day}",
    response_model=PublicTradeDayResponse,
)
def public_performance_trades(
    reporting_day: date,
    request: Request,
    response: Response,
) -> PublicTradeDayResponse:
    """Fast per-day trade evidence for the public calendar drill-down."""
    if reporting_day < _PUBLIC_TRADE_DETAIL_START:
        response.headers["Cache-Control"] = "public, max-age=60"
        return PublicTradeDayResponse(
            day=reporting_day,
            trades=(),
            updated_at=datetime.now(UTC),
        )

    service = _performance_service(request)
    user_id = _owner_reference_user_id(service)
    trades = _public_trades_for_day(service, user_id, reporting_day)
    response.headers["Cache-Control"] = "public, max-age=5, stale-while-revalidate=30"
    return PublicTradeDayResponse(
        day=reporting_day,
        trades=trades,
        updated_at=datetime.now(UTC),
    )


@router.get("/public-performance", response_model=PublicPerformanceResponse)
async def public_performance(
    request: Request,
    response: Response,
) -> PublicPerformanceResponse:
    """Provider-hidden public ledger feed for smartsignals.site.

    No login is required. The feed contains only aggregate realised P/L and executed
    trade outcome metadata; provider identity, users and broker credentials are never
    exposed.
    """
    service = _performance_service(request)
    user_id = _owner_reference_user_id(service)
    daily = _public_daily(service, user_id)
    trades = _public_trades(service, user_id)
    current = round(float(_canonical_displayed_balance(service, user_id)), 2)
    account_value, mt5_balance, account_value_updated_at = _latest_owner_account_value(
        service,
        user_id,
    )
    total = round(current - _HISTORICAL_STARTING_BALANCE, 2)
    return_percent = round(total / _HISTORICAL_STARTING_BALANCE * 100, 2)
    response.headers["Cache-Control"] = "public, max-age=5, stale-while-revalidate=30"
    return PublicPerformanceResponse(
        current_recorded_balance=current,
        total_recorded_pnl=total,
        return_percent=return_percent,
        current_account_value=(
            round(account_value, 2) if account_value is not None else None
        ),
        current_mt5_balance=(
            round(mt5_balance, 2) if mt5_balance is not None else None
        ),
        account_value_updated_at=account_value_updated_at,
        daily=daily,
        trades=trades,
        updated_at=datetime.now(UTC),
    )
