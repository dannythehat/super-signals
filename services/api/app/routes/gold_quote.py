"""Free public XAU/USD spot quote for the dashboard.

This route is deliberately isolated from MetaAPI and the broker account. It reads only
keyless public gold feeds, with Gold API as primary and XAUS as fallback, so the dashboard
price cannot consume trading API credits or touch MT5 account state.
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
_PRIMARY_URL = "https://api.gold-api.com/price/XAU"
_FALLBACK_URL = "https://xaus.com/api/v1/spot?compact=1"
_LIVE_CACHE_SECONDS = 1.0
_RETRY_CACHE_SECONDS = 3.0
_STALE_AFTER_SECONDS = 90.0


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


def _is_stale(timestamp: datetime | None, *, now: datetime) -> bool:
    if timestamp is None:
        return False
    return max(0.0, (now - timestamp).total_seconds()) > _STALE_AFTER_SECONDS


def _gold_api_quote(payload: dict[str, object], *, now: datetime) -> GoldQuoteResponse:
    price = _positive_float(payload.get("price"))
    timestamp = _quote_time(payload.get("updatedAt") or payload.get("updated_at"))
    return GoldQuoteResponse(
        price=price,
        bid=_positive_float(payload.get("bid")),
        ask=_positive_float(payload.get("ask")),
        quote_time=timestamp,
        read_at=now,
        available=price is not None,
        stale=_is_stale(timestamp, now=now),
        source="Gold API",
    )


def _xaus_quote(payload: dict[str, object], *, now: datetime) -> GoldQuoteResponse:
    price = _positive_float(payload.get("spot_usd_oz"))
    if price is None:
        xau = payload.get("xau")
        if isinstance(xau, dict):
            price = _positive_float(xau.get("price"))
    timestamp = _quote_time(
        payload.get("price_as_of") or payload.get("updated_at") or payload.get("updatedAt")
    )
    data_state = payload.get("data_state")
    upstream_stale = False
    if isinstance(data_state, dict):
        upstream_stale = str(data_state.get("status") or "").lower() == "stale"
    return GoldQuoteResponse(
        price=price,
        bid=None,
        ask=None,
        quote_time=timestamp,
        read_at=now,
        available=price is not None,
        stale=upstream_stale or _is_stale(timestamp, now=now),
        source="XAUS",
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
        source="Public gold feeds",
    )


def _cache(request: Request) -> dict[str, tuple[float, GoldQuoteResponse]]:
    existing = getattr(request.app.state, "gold_quote_cache", None)
    if isinstance(existing, dict):
        return existing
    created: dict[str, tuple[float, GoldQuoteResponse]] = {}
    request.app.state.gold_quote_cache = created
    return created


def _store(
    cache: dict[str, tuple[float, GoldQuoteResponse]],
    quote: GoldQuoteResponse,
) -> GoldQuoteResponse:
    ttl = _LIVE_CACHE_SECONDS if quote.available else _RETRY_CACHE_SECONDS
    cache[_SYMBOL] = (monotonic() + ttl, quote)
    return quote


async def _read_json(url: str, *, cache_bust: int) -> dict[str, object]:
    separator = "&" if "?" in url else "?"
    timeout = httpx.Timeout(3.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(
            f"{url}{separator}_={cache_bust}",
            headers={"Accept": "application/json", "User-Agent": "SuperSignals/1.0"},
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
    cache_bust = int(now.timestamp() * 1000)

    quote: GoldQuoteResponse | None = None
    try:
        quote = _gold_api_quote(
            await _read_json(_PRIMARY_URL, cache_bust=cache_bust),
            now=now,
        )
        if not quote.available:
            quote = None
    except (httpx.HTTPError, ValueError, TypeError):
        quote = None

    if quote is None:
        try:
            quote = _xaus_quote(
                await _read_json(_FALLBACK_URL, cache_bust=cache_bust),
                now=now,
            )
            if not quote.available:
                quote = None
        except (httpx.HTTPError, ValueError, TypeError):
            quote = None

    if quote is None:
        if previous is not None and previous.price is not None:
            quote = previous.model_copy(update={"read_at": now, "stale": True})
        else:
            quote = _unavailable(now)

    response.headers["Cache-Control"] = "no-store"
    return _store(cache, quote)
