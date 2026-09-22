"""Live XAU/USD quote plus a privacy-safe public performance feed.

The Gold quote remains isolated from MetaAPI and the user's broker account. The public
performance feed is read-only and exposes only the Owner reference ledger needed by the
Smart Signals public website: daily realised P/L plus provider-hidden trade outcomes.
Audited reporting overrides are applied to the public cash ledger without changing the
immutable broker evidence, and revoked providers remain outside user-facing performance.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
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
    daily: tuple[PublicDailyPnlResponse, ...]
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
    accounting = CanonicalTradingAccountingService(service._session_factory)
    with service._session_factory() as session:
        row = session.execute(
            text(
                """
                SELECT last_confirmed_balance
                FROM mt5_accounts
                WHERE owner_user_id=:user_id
                  AND status<>'revoked'
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"user_id": user_id},
        ).mappings().first()
    broker_balance = row["last_confirmed_balance"] if row is not None else Decimal("0")
    return accounting.displayed_balance(
        user_id,
        broker_balance=broker_balance,
    )


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

    values = {
        row["local_day"]: Decimal(str(row["pnl"] or 0))
        for row in rows
    }
    for row in reviewed_rows:
        reporting_day = row["local_day"]
        values[reporting_day] = values.get(reporting_day, Decimal("0")) + Decimal(str(row["pnl"] or 0))
    for reporting_day, reviewed_cash in reviewed_by_day.items():
        values[reporting_day] = values.get(reporting_day, Decimal("0")) + reviewed_cash

    return tuple(
        PublicDailyPnlResponse(day=day, pnl=round(float(pnl), 2))
        for day, pnl in sorted(values.items())
    )


def _public_trades(service: Day33PerformanceLedgerServiceV2, user_id: UUID) -> tuple[PublicTradeResponse, ...]:
    zone = ZoneInfo(_PUBLIC_TIMEZONE)
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
        local_day = aware.astimezone(zone).date()
        if local_day < _PUBLIC_TRADE_DETAIL_START:
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
    total = round(current - _HISTORICAL_STARTING_BALANCE, 2)
    return_percent = round(total / _HISTORICAL_STARTING_BALANCE * 100, 2)
    response.headers["Cache-Control"] = "public, max-age=5, stale-while-revalidate=30"
    return PublicPerformanceResponse(
        current_recorded_balance=current,
        total_recorded_pnl=total,
        return_percent=return_percent,
        daily=daily,
        trades=trades,
        updated_at=datetime.now(UTC),
    )
