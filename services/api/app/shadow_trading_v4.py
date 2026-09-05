"""Public-feed Provider Lab runtime with no always-on broker market-data stream.

Provider research must never keep the owner's MetaAPI/MT5 connection active merely to
observe XAUUSD. One lightweight public bid/ask snapshot is shared across every active
shadow trade. Tight scalps remain stored but cannot qualify from snapshot-resolution
market data; provider_fairness deliberately requires tick evidence for those outcomes.

The public feed is queried only when at least one shadow trade is pending/open. Database
evaluation is moved to a worker thread so research traffic cannot block FastAPI's event
loop or make the owner dashboard feel sticky.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from uuid import UUID

import httpx
from sqlalchemy import text

from app.provider_adaptive_profile import AdaptiveProviderProfileService
from app.shadow_trading_v3 import ShadowTradeManager as _BaseShadowTradeManager
from app.shadow_trading_v2 import _decimal

logger = logging.getLogger(__name__)

_PUBLIC_XAUUSD_URL = "https://biquote.io/api/XAUUSD?allowStale=false"
_PUBLIC_TIMEOUT_SECONDS = 2.0


class ShadowTradeManager(_BaseShadowTradeManager):
    """Evaluate Provider Lab from one public XAUUSD snapshot, never a MetaAPI stream."""

    async def start(self) -> None:
        # Deliberately start only the shared public-feed evaluator. Do NOT call the
        # inherited start(), because that also starts the MetaAPI websocket stream.
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(
                self._run(),
                name="super-signals-shadow-public-gold",
            )
        # Provider-language learning must not depend on the Telegram listener being
        # enabled at this exact startup. Backfill every monitored provider from the
        # durable database in an independent worker; failures never affect trading.
        asyncio.create_task(
            self._backfill_adaptive_profiles_once(),
            name="super-signals-adaptive-provider-backfill",
        )

    async def _backfill_adaptive_profiles_once(self) -> None:
        try:
            count = await asyncio.to_thread(self._backfill_adaptive_profiles_sync)
            logger.info("Adaptive Provider Lab profiles backfilled for %d sources", count)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Adaptive Provider Lab startup backfill failed safely")

    def _backfill_adaptive_profiles_sync(self) -> int:
        service = AdaptiveProviderProfileService(self._session_factory)
        with self._session_factory() as session:
            source_ids = session.execute(
                text(
                    """
                    SELECT id FROM sources
                    WHERE status IN ('testing','shadow','live')
                    ORDER BY created_at,id
                    """
                )
            ).scalars().all()
        completed = 0
        for value in source_ids:
            source_id = UUID(str(value))
            service.invalidate(source_id)
            service.get(source_id)
            completed += 1
        return completed

    async def poll_once(self) -> int:
        # No active research trades means no market-data request at all.
        rows = await asyncio.to_thread(self._active_rows)
        if not rows:
            return 0

        bid, ask = await self._public_bid_ask()
        if bid is None or ask is None:
            logger.warning("Provider Lab public XAUUSD quote unavailable")
            return 0

        # snapshot_poll is intentionally non-qualifying for scalpers. The fairness
        # policy keeps those observations for audit while refusing to manufacture a
        # precise scalp score from coarse market data.
        return await self._evaluate_all(
            bid=bid,
            ask=ask,
            quote_mode="snapshot_poll",
        )

    async def _public_bid_ask(self) -> tuple[Decimal | None, Decimal | None]:
        try:
            timeout = httpx.Timeout(_PUBLIC_TIMEOUT_SECONDS)
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(
                    _PUBLIC_XAUUSD_URL,
                    headers={
                        "Accept": "application/json",
                        "Cache-Control": "no-cache",
                        "User-Agent": "SuperSignals-ProviderLab/1.0",
                    },
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError, TypeError):
            return None, None

        if not isinstance(payload, dict):
            return None, None
        if bool(payload.get("stale")):
            return None, None
        market_state = str(payload.get("marketState") or "").strip().lower()
        if market_state == "closed":
            return None, None

        bid = _decimal(payload.get("bid"))
        ask = _decimal(payload.get("ask"))
        if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
            return None, None
        return bid, ask

    async def _evaluate_all(self, *, bid: Decimal, ask: Decimal, quote_mode: str) -> int:
        async with self._evaluation_lock:
            return await asyncio.to_thread(
                self._evaluate_all_sync,
                bid,
                ask,
                quote_mode,
            )

    def _evaluate_all_sync(self, bid: Decimal, ask: Decimal, quote_mode: str) -> int:
        rows = self._active_rows()
        if not rows:
            return 0
        changed = 0
        with self._session_factory() as session:
            for row in rows:
                changed += int(
                    self._evaluate_row(
                        session,
                        row,
                        bid=bid,
                        ask=ask,
                        quote_mode=quote_mode,
                    )
                )
            session.commit()
        return changed


__all__ = ["ShadowTradeManager"]
