"""Live XAU/USD dashboard quote from a free public market-data feed.

This route is completely isolated from MetaAPI and the user's broker account. The primary
source is biquote's live XAUUSD MT5 market-data feed; Gold API is fallback only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from math import isfinite
from time import monotonic
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from app.access_control import get_current_identity

router = APIRouter(prefix="/dashboard/gold-quote", tags=["dashboard-gold-quote"])
Identity = Annotated[dict[str, Any], Depends(get_current_identity)]

_SYMBOL = "XAUUSD"
_PRIMARY_URL = "https://biquote.io/api/XAUUSD?allowStale=false"
_FALLBACK_URL = "https://api.gold-api.com/price/XAU"
_LIVE_CACHE_SECONDS = 0.75
_RETRY_CACHE_SECONDS = 2.0


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


@router.get("", response_model=GoldQuoteResponse)
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
