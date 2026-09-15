"""Turn recorded provider trades into per-group P&L using retrospective price history.

The question this answers is "which of these groups makes money", and it is deliberately
not the question ``score_eligibility`` answers. That gate admits only bars AIDY observed
at the time, because a provider may only be *promoted* on evidence the system actually
saw. Nothing here relaxes it: scores land in ``provider_trade_scores``, which the
promotion gates never read and whose ``forward_evidence_eligible`` column is
CHECK-constrained false.

Trades come from ``provider_trade_observations`` rather than ``shadow_trades``. Shadow
trades only exist where a trade was also mirrorable, so the three largest providers by
volume have none: scoring from there would be silent about exactly the groups that most
need judging.

Replay is the live resolver's ``replay_bars``, not a second implementation. Intrabar
ambiguity, partial fills and gap handling all carry judgement, and a research figure
that did not mean the same thing as a live one would be worse than no figure.

Two conventions are applied uniformly to every provider, so that comparisons between
them stay fair:

* **A posted range is a limit.** "BUY 4290-4295" fills when price trades into the range,
  at the edge least favourable to the trader. It is never assumed to fill at the good
  edge, and a range price never appears as an immediate fill just because the message
  also said "now".
* **A bar that touches both the stop and a target is read as the stop.** M1 cannot order
  two touches inside one minute, and the live resolver refuses to score such a bar for
  scalpers rather than guess. Refusing is right when the figure will promote a provider
  and wrong here, where the question is what the trades were worth, so this takes the
  worse of the two readings instead. It never flatters a provider and it is applied to
  all of them.
* **Management is not modelled.** Providers post stop moves and early closes as separate
  messages, and outside the signal pipeline there is nothing reliably linking an update
  to the trade it amends. Every trade is therefore run to its stop, its targets, or the
  end of the follow window. A group that actively protects trades will score worse here
  than it deserves, and ``provider_trade_update_rate`` in the scoreboard is what says
  whose figure that caveat bites hardest.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from app.aidy_market_client import AidyMarketClient
from app.aidy_shadow_resolver import (
    OriginalGeometry,
    _initial_state,
    contiguous_bars,
    lifecycle_watermark,
    replay_bars,
)
from app.provider_fairness import BENCHMARK_MODEL
from app.shadow_trading_v2 import _benchmark_pnl_usd

logger = logging.getLogger(__name__)

# A trade is followed for at most this long. Beyond it the provider has effectively
# abandoned the position, and any figure would describe how long we watched rather than
# how the trade went.
MAX_TRADE_LIFETIME = timedelta(days=3)
# One research fetch covers at most this much; a longer trade is walked in steps.
FETCH_WINDOW = timedelta(hours=12)
# Legs carry no lifecycle events here, so they are all ordinary targets.
_NO_EVENTS: list[dict[str, Any]] = []


@dataclass(frozen=True, slots=True)
class TradeScore:
    observation_id: UUID
    source_id: UUID
    outcome: str
    unresolvable_reason: str | None
    entry_convention: str
    entry_price: Decimal | None
    net_pnl_usd: Decimal | None
    realized_r: Decimal | None
    legs_resolved: int
    legs_total: int
    first_bar_utc: datetime | None
    last_bar_utc: datetime | None
    bars_replayed: int
    missing_minutes: int


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result > 0 else None


def build_geometry_payload(observation: dict[str, Any]) -> dict[str, Any]:
    """Describe a recorded trade the way the resolver expects to be handed one.

    Raises ``ValueError`` naming the missing or contradictory part, which the caller
    records as the reason the trade could not be scored.
    """
    side = str(observation.get("side") or "").upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError("side_missing")

    entry_low = _decimal_or_none(observation.get("entry_low"))
    if entry_low is None:
        raise ValueError("entry_missing")
    entry_high = _decimal_or_none(observation.get("entry_high")) or entry_low
    low, high = min(entry_low, entry_high), max(entry_low, entry_high)

    stop = _decimal_or_none(observation.get("stop_loss"))
    if stop is None:
        raise ValueError("stop_loss_missing")

    raw_targets = observation.get("take_profits")
    if not isinstance(raw_targets, list):
        raise ValueError("take_profits_missing")
    targets: list[Decimal] = []
    for item in raw_targets:
        target = _decimal_or_none(item)
        # A target level on the wrong side of the entry is a misread, not a trade. It
        # would otherwise register as an instant win on the first bar.
        if target is None or target in targets:
            continue
        if side == "BUY" and target <= high:
            continue
        if side == "SELL" and target >= low:
            continue
        targets.append(target)
    if not targets:
        raise ValueError("no_target_beyond_entry")
    targets.sort(reverse=side == "SELL")

    # An exact price with an explicit market order is the one case where the fill is
    # known. Everything else is read as a range, including a range the message called a
    # market order: "buy between 4290 and 4295" states a limit however it is labelled,
    # and treating it as an immediate fill would invent a price nobody posted.
    order_type = str(observation.get("order_type") or "").lower()
    convention = "market" if order_type == "market" and low == high else "zone"

    return {
        "side": side,
        "entry_order_type": convention,
        "entry_low": str(low),
        "entry_high": str(high),
        "initial_stop": str(stop),
        "entry_index": 1,
        "legs": [
            {"tp_index": index, "target_price": str(target), "is_runner": False}
            for index, target in enumerate(targets, start=1)
        ],
    }


def classify(state: Any, *, net_pnl: Decimal) -> str:
    """Name the outcome the way a reader of the scoreboard would.

    A trade still running when the follow window closes is reported as such rather than
    counted either way, because its result is not known and forcing it into a column
    would bias the provider's figure in whichever direction we chose.
    """
    if state.status in {"cancelled", "missed"} or (
        state.status == "pending" and state.entry_price is None
    ):
        return "never_entered"
    if not state.terminal:
        return "open_at_window_end"
    if net_pnl > 0:
        return "won"
    if net_pnl < 0:
        return "lost"
    return "breakeven"


class ProviderTradeScorer:
    """Replay one recorded trade against retrospective bars."""

    def __init__(self, *, client: AidyMarketClient) -> None:
        self._client = client

    def _unscored(
        self, observation: dict[str, Any], reason: str, *, legs_total: int = 0
    ) -> TradeScore:
        return TradeScore(
            observation_id=UUID(str(observation["id"])),
            source_id=UUID(str(observation["source_id"])),
            outcome="unresolvable",
            unresolvable_reason=reason[:120],
            entry_convention="zone",
            entry_price=None,
            net_pnl_usd=None,
            realized_r=None,
            legs_resolved=0,
            legs_total=legs_total,
            first_bar_utc=None,
            last_bar_utc=None,
            bars_replayed=0,
            missing_minutes=0,
        )

    async def score(self, observation: dict[str, Any]) -> TradeScore:
        observed_at = observation.get("observed_at")
        if not isinstance(observed_at, datetime):
            return self._unscored(observation, "observed_at_missing")
        observed_at = observed_at.astimezone(UTC)

        try:
            payload = build_geometry_payload(observation)
            geometry = OriginalGeometry.from_payload(payload)
        except ValueError as exc:
            return self._unscored(observation, str(exc))

        convention = str(payload["entry_order_type"])
        leg_ids = {index: UUID(int=index) for index in sorted(geometry.targets)}
        state = _initial_state(
            geometry=geometry,
            leg_ids=leg_ids,
            signal_posted_at=observed_at,
            lifecycle_mark=lifecycle_watermark([]),
        )

        cursor = observed_at.replace(second=0, microsecond=0)
        # Bars still forming are not settled history, so the window stops short of now.
        settled = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=5)
        deadline = min(cursor + MAX_TRADE_LIFETIME, settled)

        bars_replayed = 0
        missing_minutes = 0
        first_bar: datetime | None = None
        last_bar: datetime | None = None

        while cursor < deadline and not state.terminal:
            end = min(cursor + FETCH_WINDOW, deadline)
            try:
                window = await self._client.fetch_research_m1(start=cursor, end=end)
            except Exception as exc:  # noqa: BLE001 - one bad window must not end a sweep
                logger.info(
                    "research fetch failed observation=%s window=%s..%s error=%s",
                    observation.get("id"),
                    cursor.isoformat(),
                    end.isoformat(),
                    type(exc).__name__,
                )
                if bars_replayed == 0:
                    return self._unscored(
                        observation,
                        f"research_fetch_failed:{type(exc).__name__}",
                        legs_total=len(geometry.targets),
                    )
                break

            missing_minutes += len(window.missing_open_times)
            usable, _first_missing = contiguous_bars(window, after_cursor=None)
            if not usable:
                # The record stops here. Skipping the gap and replaying what comes after
                # would assert a continuity the bars do not support.
                break

            state = replay_bars(
                state=state,
                geometry=geometry,
                events=_NO_EVENTS,
                bars=usable,
                signal_posted_at=observed_at,
                sibling_entries=[],
                full_replay=(bars_replayed == 0),
                # False, even for scalpers: the strict path refuses to score a bar that
                # touches stop and target together, which is right when the figure will
                # promote a provider and useless when the question is what the trades
                # were worth. Non-strict takes the stop, which is the worse reading.
                strict_intrabar_ambiguity=False,
            )
            bars_replayed += len(usable)
            if first_bar is None:
                first_bar = usable[0].open_time_utc
            last_bar = usable[-1].open_time_utc
            cursor = last_bar + timedelta(minutes=1)

        if bars_replayed == 0:
            return self._unscored(
                observation,
                "no_price_history_for_window",
                legs_total=len(geometry.targets),
            )
        if state.score_block_reason:
            return self._unscored(
                observation, state.score_block_reason, legs_total=len(state.legs)
            )

        realized_r = sum((leg.realized_r for leg in state.legs), Decimal("0"))
        net_pnl = sum(
            (_benchmark_pnl_usd(leg.realized_r) for leg in state.legs), Decimal("0")
        )
        outcome = classify(state, net_pnl=net_pnl)
        entered = outcome != "never_entered"

        return TradeScore(
            observation_id=UUID(str(observation["id"])),
            source_id=UUID(str(observation["source_id"])),
            outcome=outcome,
            unresolvable_reason=None,
            entry_convention=convention,
            entry_price=state.entry_price if entered else None,
            net_pnl_usd=net_pnl if entered else None,
            realized_r=realized_r if entered else None,
            legs_resolved=sum(1 for leg in state.legs if leg.status == "closed"),
            legs_total=len(state.legs),
            first_bar_utc=first_bar,
            last_bar_utc=last_bar,
            bars_replayed=bars_replayed,
            missing_minutes=missing_minutes,
        )


__all__ = [
    "FETCH_WINDOW",
    "MAX_TRADE_LIFETIME",
    "BENCHMARK_MODEL",
    "ProviderTradeScorer",
    "TradeScore",
    "build_geometry_payload",
    "classify",
]
