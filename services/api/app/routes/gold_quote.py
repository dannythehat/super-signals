"""Free public XAU/USD spot quote for the dashboard.

This route is intentionally isolated from MetaAPI and the broker account. Dashboard gold
prices come from the keyless Gold API public real-time endpoint, so displaying a price
cannot consume trading API credits or touch MT5 account state.
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
_SOURCE = "Gold API"
_UPSTREAM_URL = "https://api.gold-api.com/price/XAU"
_LIVE_CACHE_SECONDS = 5.0
_RETRY_CACHE_SECONDS = 30.0
_STALE_AFTER_SECONDS = 60.0


class GoldQuoteResponse(BaseModel):
    symbol: str = _SYMBOL
    price: float | None
    bid: float | None
    ask: float | None
    quote_time: datetime | None
    read_at: datetime
    available: bool
    stale: bool
    source: str = _SOURCE


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


def _quote_from_payload(payload: dict[str, object], *, now: datetime) -> GoldQuoteResponse:
    price = _positive_float(payload.get("price"))
    bid = _positive_float(payload.get("bid"))
    ask = _positive_float(payload.get("ask"))
    timestamp = _quote_time(payload.get("updatedAt") or payload.get("updated_at"))
    stale = (
        timestamp is None
        or max(0.0, (now - timestamp).total_seconds()) > _STALE_AFTER_SECONDS
    )
    return GoldQuoteResponse(
        price=price,
        bid=bid,
        ask=ask,
        quote_time=timestamp,
        read_at=now,
        available=price is not None,
        stale=stale,
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
    key: str,
    quote: GoldQuoteResponse,
) -> GoldQuoteResponse:
    ttl = _LIVE_CACHE_SECONDS if quote.available and not quote.stale else _RETRY_CACHE_SECONDS
    cache[key] = (monotonic() + ttl, quote)
    return quote


async def _read_free_gold_price() -> dict[str, object]:
    timeout = httpx.Timeout(3.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(
            _UPSTREAM_URL,
            headers={
                "Accept": "application/json",
                "User-Agent": "SuperSignals/1.0",
            },
        )
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("invalid_gold_api_response")
    return payload


@router.get("", response_model=GoldQuoteResponse)
async def account_gold_quote(
    request: Request,
    response: Response,
    _identity: Identity,
) -> GoldQuoteResponse:
    now = datetime.now(UTC)
    key = _SYMBOL
    cache = _cache(request)
    cached = cache.get(key)
    if cached is not None and monotonic() < cached[0]:
        response.headers["Cache-Control"] = "private, max-age=2"
        return cached[1]

    previous = cached[1] if cached is not None else None
    try:
        payload = await _read_free_gold_price()
        quote = _quote_from_payload(payload, now=now)
    except (httpx.HTTPError, ValueError, TypeError):
        if previous is not None and previous.price is not None:
            quote = previous.model_copy(update={"read_at": now, "stale": True})
        else:
            quote = _unavailable(now)

    response.headers["Cache-Control"] = "private, max-age=2"
    return _store(cache, key, quote)
