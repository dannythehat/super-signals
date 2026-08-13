"""Day 25 one-time price and all-or-nothing funds preflight.

Super Signals is a signal follower. Day 25 does not reinterpret a provider's
entry, stop loss, take profit or trade thesis. It consumes one already-fresh Day
23 live-state snapshot, checks the stated entry once, then checks whether the
complete Day 24-sized TP set can be funded. It never places a trade.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal

from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_margin_gateway import MetaApiMarginGateway
from app.mt5_read_service_day23 import Day23LiveState, Day23Mt5ReadService, Day23ReadError
from app.risk_sizing_day24 import Day24RiskSizingResult

logger = logging.getLogger(__name__)

# One bounded retry only. Margin truth stays mandatory; this exists so a single
# transient MetaAPI blip cannot permanently discard a valid provider signal.
_MARGIN_ATTEMPTS = 2
_MARGIN_RETRY_DELAY_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class Day25PreflightResult:
    proceed: bool
    block_reason: str | None
    side: str
    symbol: str
    signal_entry_price: Decimal
    executable_price: Decimal | None
    entry_available: bool
    free_margin: Decimal
    required_margin: Decimal | None
    position_count: int
    position_volume: Decimal
    total_volume: Decimal
    positions_allowed: int
    price_check_count: int
    margin_check_count: int
    all_or_nothing: bool
    trade_action_created: bool


class Day25TradePreflightService:
    """Apply only the locked one-time entry and whole-signal funds gates."""

    def __init__(self, *, margin_gateway: MetaApiMarginGateway) -> None:
        self._margin_gateway = margin_gateway

    async def evaluate(
        self,
        *,
        live_state: Day23LiveState,
        side: str,
        sizing: Day24RiskSizingResult,
        token: str,
    ) -> Day25PreflightResult:
        normalized_side = side.strip().upper()
        if normalized_side not in {"BUY", "SELL"}:
            raise ValueError("trade_side_invalid")

        symbol = live_state.price.symbol.strip().upper()
        signal_entry = sizing.signal_entry_price
        free_margin = self._decimal(live_state.account.free_margin)
        total_volume = sizing.volume * Decimal(sizing.position_count)

        if total_volume <= 0:
            raise ValueError("total_volume_invalid")

        try:
            executable_price = Decimal(
                str(Day23Mt5ReadService.executable_price(live_state, normalized_side))
            )
        except Day23ReadError as exc:
            return self._blocked(
                reason=exc.code,
                side=normalized_side,
                symbol=symbol,
                signal_entry=signal_entry,
                executable_price=None,
                entry_available=False,
                free_margin=free_margin,
                required_margin=None,
                sizing=sizing,
                total_volume=total_volume,
                price_check_count=1,
                margin_check_count=0,
            )

        # Literal provider-following rule: the stated entry is checked once.
        # A different executable price is not labelled better/worse and is not
        # substituted. It simply means the stated entry is unavailable now.
        entry_available = executable_price == signal_entry
        if not entry_available:
            return self._blocked(
                reason="entry_price_unavailable",
                side=normalized_side,
                symbol=symbol,
                signal_entry=signal_entry,
                executable_price=executable_price,
                entry_available=False,
                free_margin=free_margin,
                required_margin=None,
                sizing=sizing,
                total_volume=total_volume,
                price_check_count=1,
                margin_check_count=0,
            )

        if not live_state.account.trade_allowed:
            return self._blocked(
                reason="trading_not_allowed",
                side=normalized_side,
                symbol=symbol,
                signal_entry=signal_entry,
                executable_price=executable_price,
                entry_available=True,
                free_margin=free_margin,
                required_margin=None,
                sizing=sizing,
                total_volume=total_volume,
                price_check_count=1,
                margin_check_count=0,
            )

        # A transient MetaAPI failure must not silently lose a genuine provider
        # signal, but margin truth is still mandatory. The gateway already marks
        # timeout / unreachable / 408 / 425 / 429 / 5xx as retryable; every other
        # code (400/401/403/404, invalid response) is a real condition that must
        # fail closed immediately with no retry. This mirrors the bounded
        # single-retry pattern already used by the Day 26/27/36 broker paths.
        required_margin: Decimal | None = None
        margin_check_count = 0
        last_error: MetaApiGatewayError | None = None
        for attempt in range(1, _MARGIN_ATTEMPTS + 1):
            margin_check_count = attempt
            try:
                required_margin = self._decimal(
                    await self._margin_gateway.calculate_margin(
                        token=token,
                        account_id=live_state.metaapi_account_id,
                        region=live_state.region,
                        symbol=symbol,
                        side=normalized_side,
                        volume=float(total_volume),
                        open_price=float(executable_price),
                    )
                )
                last_error = None
                break
            except MetaApiGatewayError as exc:
                last_error = exc
                if not exc.retryable or attempt == _MARGIN_ATTEMPTS:
                    break
                logger.warning(
                    "Margin preflight retrying after transient failure code=%s attempt=%d",
                    exc.code,
                    attempt,
                )
                await asyncio.sleep(_MARGIN_RETRY_DELAY_SECONDS)

        if last_error is not None or required_margin is None:
            # Preserve the existing fail-closed public contract while making the
            # sanitized MetaAPI reason visible to operators. No token, account ID,
            # balance or provider data is logged here.
            logger.warning(
                "Margin preflight unavailable code=%s attempts=%d",
                last_error.code if last_error is not None else "metaapi_invalid_response",
                margin_check_count,
            )
            return self._blocked(
                reason="margin_check_unavailable",
                side=normalized_side,
                symbol=symbol,
                signal_entry=signal_entry,
                executable_price=executable_price,
                entry_available=True,
                free_margin=free_margin,
                required_margin=None,
                sizing=sizing,
                total_volume=total_volume,
                price_check_count=1,
                margin_check_count=margin_check_count,
            )

        if required_margin > free_margin:
            return self._blocked(
                reason="insufficient_funds",
                side=normalized_side,
                symbol=symbol,
                signal_entry=signal_entry,
                executable_price=executable_price,
                entry_available=True,
                free_margin=free_margin,
                required_margin=required_margin,
                sizing=sizing,
                total_volume=total_volume,
                price_check_count=1,
                margin_check_count=margin_check_count,
            )

        return Day25PreflightResult(
            proceed=True,
            block_reason=None,
            side=normalized_side,
            symbol=symbol,
            signal_entry_price=signal_entry,
            executable_price=executable_price,
            entry_available=True,
            free_margin=free_margin,
            required_margin=required_margin,
            position_count=sizing.position_count,
            position_volume=sizing.volume,
            total_volume=total_volume,
            positions_allowed=sizing.position_count,
            price_check_count=1,
            margin_check_count=margin_check_count,
            all_or_nothing=True,
            trade_action_created=False,
        )

    @classmethod
    def _blocked(
        cls,
        *,
        reason: str,
        side: str,
        symbol: str,
        signal_entry: Decimal,
        executable_price: Decimal | None,
        entry_available: bool,
        free_margin: Decimal,
        required_margin: Decimal | None,
        sizing: Day24RiskSizingResult,
        total_volume: Decimal,
        price_check_count: int,
        margin_check_count: int,
    ) -> Day25PreflightResult:
        return Day25PreflightResult(
            proceed=False,
            block_reason=reason,
            side=side,
            symbol=symbol,
            signal_entry_price=signal_entry,
            executable_price=executable_price,
            entry_available=entry_available,
            free_margin=free_margin,
            required_margin=required_margin,
            position_count=sizing.position_count,
            position_volume=sizing.volume,
            total_volume=total_volume,
            positions_allowed=0,
            price_check_count=price_check_count,
            margin_check_count=margin_check_count,
            all_or_nothing=True,
            trade_action_created=False,
        )

    @staticmethod
    def _decimal(value: object) -> Decimal:
        if isinstance(value, bool):
            raise ValueError("numeric_value_invalid")
        try:
            parsed = Decimal(str(value))
        except Exception as exc:
            raise ValueError("numeric_value_invalid") from exc
        if not parsed.is_finite():
            raise ValueError("numeric_value_invalid")
        return parsed
