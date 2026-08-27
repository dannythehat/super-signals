"""Day 25 one-time broker-read preflight before a provider market order.

Super Signals is a signal follower. This stage may verify that the terminal can trade and
that the executable quote being used is still the authorised market price. It must NOT
turn account balance, equity, free margin or an advisory margin calculation into a local
trade veto.

Risk is sized independently per provider section/leg by Day 24. The actual Vantage/MT5
order request is the sole authority on whether the broker can accept that order. This
keeps paper and future LIVE execution identical and removes an unnecessary network call
from the time-critical entry path.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.metaapi_margin_gateway import MetaApiMarginGateway
from app.mt5_read_service_day23 import Day23LiveState, Day23Mt5ReadService, Day23ReadError
from app.risk_sizing_day24 import Day24RiskSizingResult


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
    """Verify quote/trading availability only; never impose a local funds veto."""

    def __init__(self, *, margin_gateway: MetaApiMarginGateway) -> None:
        # Retained in the constructor for API compatibility with the execution service.
        # It is deliberately not called from evaluate(): advisory margin must neither
        # block nor delay a provider trade before the real broker order is attempted.
        self._margin_gateway = margin_gateway

    async def evaluate(
        self,
        *,
        live_state: Day23LiveState,
        side: str,
        sizing: Day24RiskSizingResult,
        token: str,
    ) -> Day25PreflightResult:
        del token
        normalized_side = side.strip().upper()
        if normalized_side not in {"BUY", "SELL"}:
            raise ValueError("trade_side_invalid")

        symbol = live_state.price.symbol.strip().upper()
        signal_entry = sizing.signal_entry_price
        free_margin = self._decimal(live_state.account.free_margin)
        total_volume = sum((item.volume for item in sizing.positions), Decimal("0"))
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
                sizing=sizing,
                total_volume=total_volume,
                price_check_count=1,
            )

        # The caller may substitute an already-authorised fresh market price into
        # sizing.signal_entry_price. Day 25 only ensures the same quote is still being
        # used; it does not reinterpret the provider's trade.
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
                sizing=sizing,
                total_volume=total_volume,
                price_check_count=1,
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
                sizing=sizing,
                total_volume=total_volume,
                price_check_count=1,
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
            required_margin=None,
            position_count=sizing.position_count,
            position_volume=sizing.volume,
            total_volume=total_volume,
            positions_allowed=sizing.position_count,
            price_check_count=1,
            margin_check_count=0,
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
        sizing: Day24RiskSizingResult,
        total_volume: Decimal,
        price_check_count: int,
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
            required_margin=None,
            position_count=sizing.position_count,
            position_volume=sizing.volume,
            total_volume=total_volume,
            positions_allowed=0,
            price_check_count=price_check_count,
            margin_check_count=0,
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
