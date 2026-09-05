"""Provider Lab runtime with AIDY M1 market truth for intraday/swing providers.

AIDY is an authenticated read-only provider, never a shared database. Intraday and
swing shadow trades are resolved incrementally from admitted AIDY M1 OHLC. Scalper
messages continue to be captured by the normal ingestion pipeline, but their shadow
rows are explicitly excluded from scoring/promotion and need no market-resolution read.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from uuid import UUID

import httpx
from sqlalchemy import text

from app.provider_adaptive_profile import AdaptiveProviderProfileService
from app.shadow_trading_v2 import _decimal
from app.shadow_trading_v3 import ShadowTradeManager as _BaseShadowTradeManager

logger = logging.getLogger(__name__)

_PUBLIC_XAUUSD_URL = "https://biquote.io/api/XAUUSD?allowStale=false"
_PUBLIC_TIMEOUT_SECONDS = 2.0
_AIDY_POLL_SECONDS = 300
_AIDY_STYLES = {"intraday", "swing_or_sparse"}


class ShadowTradeManager(_BaseShadowTradeManager):
    """Resolve Provider Lab without any always-on broker market-data stream."""

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(
                self._run(),
                name="super-signals-shadow-public-gold",
            )
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

    def _enforce_scalper_exclusion_sync(self) -> int:
        with self._session_factory() as session:
            result = session.execute(
                text(
                    """
                    UPDATE shadow_trades
                    SET score_eligible=false,
                        score_exclusion_reason='unsupported_style_scalper',
                        updated_at=now()
                    WHERE provider_style='scalper'
                      AND (
                        score_eligible
                        OR score_exclusion_reason IS DISTINCT FROM 'unsupported_style_scalper'
                      )
                    """
                )
            )
            session.commit()
            return int(result.rowcount or 0)

    async def poll_once(self) -> int:
        # This is a policy gate, not market inference: future scalper rows are kept for
        # audit/message observation but cannot silently re-enter scoring or promotion.
        await asyncio.to_thread(self._enforce_scalper_exclusion_sync)

        rows = await asyncio.to_thread(self._active_rows)
        public_rows = [
            row
            for row in rows
            if str(row["provider_style"]) not in _AIDY_STYLES | {"scalper"}
        ]
        if not public_rows:
            return 0

        bid, ask = await self._public_bid_ask()
        if bid is None or ask is None:
            logger.warning("Provider Lab public XAUUSD quote unavailable")
            return 0

        # Mixed/unknown legacy rows retain the existing public-snapshot path. The
        # explicit Step-1 canonical feed applies only to intraday/swing providers.
        return await self._evaluate_public_rows(
            public_rows,
            bid=bid,
            ask=ask,
            quote_mode="snapshot_poll",
        )

    async def _public_bid_ask(self) -> tuple[Decimal | None, Decimal | None]:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(_PUBLIC_TIMEOUT_SECONDS)) as client:
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

        if not isinstance(payload, dict) or bool(payload.get("stale")):
            return None, None
        if str(payload.get("marketState") or "").strip().lower() == "closed":
            return None, None
        bid = _decimal(payload.get("bid"))
        ask = _decimal(payload.get("ask"))
        if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
            return None, None
        return bid, ask

    async def _evaluate_public_rows(
        self,
        rows,
        *,
        bid: Decimal,
        ask: Decimal,
        quote_mode: str,
    ) -> int:
        async with self._evaluation_lock:
            return await asyncio.to_thread(
                self._evaluate_public_rows_sync,
                rows,
                bid,
                ask,
                quote_mode,
            )

    def _evaluate_public_rows_sync(self, rows, bid: Decimal, ask: Decimal, quote_mode: str) -> int:
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

    async def _evaluate_all(self, *, bid: Decimal, ask: Decimal, quote_mode: str) -> int:
        """Keep the inherited fair evaluator's synchronous DB work off the event loop."""
        async with self._evaluation_lock:
            rows = await asyncio.to_thread(self._active_rows)
            if not rows:
                return 0
            return await asyncio.to_thread(
                self._evaluate_public_rows_sync,
                rows,
                bid,
                ask,
                quote_mode,
            )


__all__ = ["ShadowTradeManager"]
