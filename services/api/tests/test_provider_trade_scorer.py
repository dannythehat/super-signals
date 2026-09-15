"""A provider's trades have to be scorable whether or not we could mirror them.

Shadow trades exist only where a trade was also executable, so on 2026-09-15 the three
largest providers by volume have none at all -- TDC V2 565 recorded trades and 0 shadow
trades, The Gold Club 508 and 0, GOLD VIP 92 and 0. These tests pin scoring to the
observation record instead, and pin the two conventions that make one provider's figure
comparable to another's: a posted range fills at the edge least favourable to the
trader, and a target on the wrong side of the entry is a misread rather than an
instant win.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.aidy_market_client import AidyM1Bar, AidyM1Window
from app.provider_trade_scorer import (
    ProviderTradeScorer,
    build_geometry_payload,
)

POSTED_AT = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(days=1)


def bar(minute: int, *, high: str, low: str, open_: str | None = None,
        close: str | None = None) -> AidyM1Bar:
    open_time = POSTED_AT + timedelta(minutes=minute)
    return AidyM1Bar(
        open_time_utc=open_time,
        open=Decimal(open_ or low),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close or high),
        revision_index=0,
        first_observed_at=open_time + timedelta(minutes=1),
        payload_digest=f"digest-{minute}",
    )


class FakeMarketClient:
    """Serves a fixed tape, and records nothing it was not asked for."""

    def __init__(self, bars: list[AidyM1Bar]) -> None:
        self._bars = {item.open_time_utc: item for item in bars}
        self.windows: list[tuple[datetime, datetime]] = []

    async def fetch_research_m1(self, *, start: datetime, end: datetime) -> AidyM1Window:
        self.windows.append((start, end))
        expected: list[datetime] = []
        cursor = start
        while cursor < end:
            expected.append(cursor)
            cursor += timedelta(minutes=1)
        present = [self._bars[when] for when in expected if when in self._bars]
        missing = tuple(when for when in expected if when not in self._bars)
        return AidyM1Window(
            start=start,
            end=end,
            bars=tuple(present),
            expected_open_times=tuple(expected),
            missing_open_times=missing,
        )


def observation(**overrides) -> dict:
    base = {
        "id": uuid4(),
        "source_id": uuid4(),
        "observed_at": POSTED_AT,
        "side": "BUY",
        "entry_low": Decimal("4000"),
        "entry_high": Decimal("4000"),
        "stop_loss": Decimal("3990"),
        "take_profits": ["4010", "4020"],
        "order_type": "market",
    }
    base.update(overrides)
    return base


def score(obs: dict, bars: list[AidyM1Bar]):
    client = FakeMarketClient(bars)
    return asyncio.run(ProviderTradeScorer(client=client).score(obs))


def test_a_winner_pays_the_benchmark_risk_on_every_leg() -> None:
    """Entry 4000, stop 3990, so 10 points is 1R and each leg risks $10."""
    result = score(
        observation(),
        [bar(0, low="3999", high="4001"), bar(1, low="4000", high="4012"),
         bar(2, low="4010", high="4021")],
    )

    assert result.outcome == "won"
    # TP1 at +1R and TP2 at +2R, $10 risked per leg.
    assert result.net_pnl_usd == Decimal("30.00")
    assert result.realized_r == Decimal("3")
    assert result.legs_resolved == 2
    assert result.legs_total == 2


def test_a_loser_loses_the_risk_on_every_open_leg() -> None:
    result = score(
        observation(),
        [bar(0, low="3999", high="4001"), bar(1, low="3989", high="4000")],
    )

    assert result.outcome == "lost"
    assert result.net_pnl_usd == Decimal("-20.00")
    assert result.legs_resolved == 2


def test_a_range_fills_at_the_edge_least_favourable_to_the_trader() -> None:
    """A BUY posted as 3995-4005 is scored as a fill at 4005, never at 3995.

    The convention is applied to every provider alike, so the comparison between them
    stays fair; picking the good edge would flatter whoever posts the widest ranges.
    """
    result = score(
        observation(
            entry_low=Decimal("3995"),
            entry_high=Decimal("4005"),
            stop_loss=Decimal("3985"),
            take_profits=["4015"],
        ),
        [bar(0, low="3998", high="4002"), bar(1, low="4001", high="4016")],
    )

    assert result.entry_price == Decimal("4002")
    assert result.outcome == "won"


def test_a_range_called_a_market_order_is_still_read_as_a_range() -> None:
    """"Buy between 4290 and 4295" states a limit however the message labelled it."""
    payload = build_geometry_payload(
        observation(entry_low=Decimal("4290"), entry_high=Decimal("4295"),
                    stop_loss=Decimal("4280"), take_profits=["4305"])
    )

    assert payload["entry_order_type"] == "zone"


def test_an_exact_market_price_is_read_as_a_market_order() -> None:
    assert build_geometry_payload(observation())["entry_order_type"] == "market"


def test_a_trade_the_market_never_reached_is_not_a_loss() -> None:
    """Never entered is its own answer. Counting it either way would be a fabrication."""
    result = score(
        observation(entry_low=Decimal("3900"), entry_high=Decimal("3905"),
                    stop_loss=Decimal("3890"), take_profits=["3930"]),
        [bar(minute, low="4000", high="4005") for minute in range(6)],
    )

    assert result.outcome == "never_entered"
    assert result.net_pnl_usd is None
    assert result.bars_replayed == 6


def test_a_target_on_the_wrong_side_of_the_entry_is_dropped() -> None:
    """Otherwise a misread target registers as a win on the first bar."""
    payload = build_geometry_payload(
        observation(take_profits=["3980", "4010"])
    )

    assert [leg["target_price"] for leg in payload["legs"]] == ["4010"]


def test_a_trade_with_no_usable_target_is_not_scored() -> None:
    result = score(observation(take_profits=["3980"]), [bar(0, low="3999", high="4001")])

    assert result.outcome == "unresolvable"
    assert result.unresolvable_reason == "no_target_beyond_entry"
    assert result.net_pnl_usd is None


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("side", "side_missing"),
        ("entry_low", "entry_missing"),
        ("stop_loss", "stop_loss_missing"),
    ],
)
def test_an_incomplete_trade_says_what_is_missing(field, reason) -> None:
    result = score(observation(**{field: None}), [bar(0, low="3999", high="4001")])

    assert result.outcome == "unresolvable"
    assert result.unresolvable_reason == reason


def test_a_day_with_no_price_history_is_reported_rather_than_guessed() -> None:
    """The days capture was down are exactly why this exists; they are not zeroes."""
    result = score(observation(), [])

    assert result.outcome == "unresolvable"
    assert result.unresolvable_reason == "no_price_history_for_window"
    assert result.net_pnl_usd is None


def test_replay_stops_where_the_record_stops() -> None:
    """A gap ends the follow. Replaying past it would assert a continuity we do not have."""
    result = score(
        observation(),
        [bar(0, low="3999", high="4001"), bar(1, low="4000", high="4002"),
         # minute 2 missing
         bar(3, low="4000", high="4030")],
    )

    assert result.outcome == "open_at_window_end"
    assert result.bars_replayed == 2
    assert result.missing_minutes > 0


def test_a_trade_still_running_is_not_counted_as_a_win_or_a_loss() -> None:
    result = score(
        observation(),
        [bar(0, low="3999", high="4001"), bar(1, low="4000", high="4012")],
    )

    assert result.outcome == "open_at_window_end"
    # The leg that did close still carries its realised figure.
    assert result.net_pnl_usd == Decimal("10.00")
    assert result.legs_resolved == 1
    assert result.legs_total == 2
