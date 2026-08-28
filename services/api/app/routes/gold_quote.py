"""Lightweight authenticated XAUUSD quote endpoint for the mobile dashboard.

This endpoint deliberately reads price only. It does not refresh account information,
positions, performance, reconciliation, or trading state, so a fast quote cadence cannot
make the main dashboard or execution path heavier.
"""

from __future__ import annotations

from datetime import UTC, datetime
from math import isfinite
from time import monotonic
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy import text

from app.access_control import get_current_identity
from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_token_scope import inspect_metaapi_token_scope
from app.mt5_connection_service_day30 import Day30Mt5ConnectionService
from app.mt5_crypto import BrokerCredentialDecryptionError
from app.mt5_runtime import require_mt5_service
from app.paper_resilient_read_gateway import ResilientMetaApiReadGateway

router = APIRouter(prefix="/dashboard/gold-quote", tags=["dashboard-gold-quote"])
Identity = Annotated[dict[str, Any], Depends(get_current_identity)]

_SYMBOL = "XAUUSD"
_LIVE_CACHE_SECONDS = 1.5
_RETRY_CACHE_SECONDS = 10.0
_STALE_AFTER_SECONDS = 15.0


class GoldQuoteResponse(BaseModel):
    symbol: str = _SYMBOL
    price: float | None
    bid: float | None
    ask: float | None
    quote_time: datetime | None
    read_at: datetime
    available: bool
    stale: bool
    source: str = "Vantage MT5"


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
    bid = _positive_float(payload.get("bid"))
    ask = _positive_float(payload.get("ask"))
    timestamp = _quote_time(payload.get("time"))
    price = ((bid + ask) / 2.0) if bid is not None and ask is not None else None
    stale = timestamp is None or max(0.0, (now - timestamp).total_seconds()) > _STALE_AFTER_SECONDS
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


def _gateway(request: Request) -> ResilientMetaApiReadGateway:
    existing = getattr(request.app.state, "gold_quote_gateway", None)
    if isinstance(existing, ResilientMetaApiReadGateway):
        return existing
    created = ResilientMetaApiReadGateway(timeout_seconds=3.0, attempts=2, retry_delay_seconds=0.15)
    request.app.state.gold_quote_gateway = created
    return created


def _store(
    cache: dict[str, tuple[float, GoldQuoteResponse]],
    key: str,
    quote: GoldQuoteResponse,
) -> GoldQuoteResponse:
    ttl = _LIVE_CACHE_SECONDS if quote.available and not quote.stale else _RETRY_CACHE_SECONDS
    cache[key] = (monotonic() + ttl, quote)
    return quote


@router.get("", response_model=GoldQuoteResponse)
async def account_gold_quote(
    request: Request,
    response: Response,
    _identity: Identity,
) -> GoldQuoteResponse:
    now = datetime.now(UTC)
    base = require_mt5_service(request)
    if not isinstance(base, Day30Mt5ConnectionService):
        return _unavailable(now)

    key = _SYMBOL
    cache = _cache(request)
    cached = cache.get(key)
    if cached is not None and monotonic() < cached[0]:
        response.headers["Cache-Control"] = "private, max-age=1"
        return cached[1]

    previous = cached[1] if cached is not None else None
    with base._session_factory() as session:
        row = session.execute(
            text(
                """
                SELECT a.metaapi_account_id, a.metaapi_token_ciphertext
                FROM mt5_accounts a
                JOIN users u ON u.id = a.owner_user_id
                JOIN user_roles ur ON ur.user_id = u.id
                JOIN roles r ON r.id = ur.role_id
                WHERE r.name = 'owner'
                  AND u.status NOT IN ('revoked', 'suspended')
                  AND a.status != 'revoked'
                ORDER BY u.created_at, u.id
                LIMIT 1
                """
            ),
        ).mappings().first()

    if row is None:
        quote = _unavailable(now)
        response.headers["Cache-Control"] = "private, max-age=1"
        return _store(cache, key, quote)

    try:
        token = base._cipher.decrypt(bytes(row["metaapi_token_ciphertext"]))
        scope = inspect_metaapi_token_scope(token)
        if (
            scope.jwt_payload_decoded
            and scope.is_explicitly_narrowed
            and not scope.has_terminal_access
        ):
            quote = _unavailable(now)
        else:
            account_id = str(row["metaapi_account_id"])
            gateway = _gateway(request)
            region = await gateway.resolve_account_region(token=token, account_id=account_id)
            payload = await gateway.read_symbol_price(
                token=token,
                account_id=account_id,
                region=region,
                symbol=_SYMBOL,
            )
            quote = _quote_from_payload(payload, now=now)
    except (BrokerCredentialDecryptionError, MetaApiGatewayError):
        if previous is not None and previous.price is not None:
            quote = previous.model_copy(update={"read_at": now, "stale": True})
        else:
            quote = _unavailable(now)

    response.headers["Cache-Control"] = "private, max-age=1"
    return _store(cache, key, quote)
