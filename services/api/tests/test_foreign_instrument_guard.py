"""A source profile fills a silence; it never overrides a named instrument.

A dedicated Gold provider's profile may supply XAUUSD when the message does not name an
instrument at all. That is deliberate: providers like TGC post "Im buying 4384" and mean
gold. But the check that allowed it only asked whether a gold token was present, so a
message naming a *different* instrument also counted as "no gold token" and the profile
filled it in anyway.

On production, SureShot GOLD -- a live-executing source -- posted:

    BTCUSD SELL 79794.4 SL: 80994.4 TP: 76194.4

and the signal was stored as XAUUSD at a Bitcoin price. No position was created, but
only because a later, unrelated price check rejected an entry thousands of dollars from
the gold market. That is luck, not a guarantee, so the instrument rule now refuses a
foreign instrument outright.
"""

from __future__ import annotations

import pytest

from app.ai_message_supervisor import AiMessageDecision
from app.v1_message_policy import apply_v1_message_policy

GOLD_PROFILE = "sureshot_xauusd"


def _decision(extracted: dict) -> AiMessageDecision:
    return AiMessageDecision(
        decision="new_trade",
        action="execute",
        confidence=0.8,
        reason="candidate",
        extracted=extracted,
        model="gpt-5-mini-2025-08-07",
        response_id=None,
        latency_ms=5,
        source="openai",
        raw_text_sha256="a" * 64,
    )


def _apply(raw_text: str, **overrides):
    extracted = {
        "side": "SELL",
        "symbol": "XAUUSD",
        "entry_low": "79794.4",
        "entry_high": "79794.4",
        "stop_loss": "80994.4",
        "take_profits": ["76194.4"],
        "order_type": "market",
        "source_profile": GOLD_PROFILE,
    }
    extracted.update(overrides)
    return apply_v1_message_policy(_decision(extracted), raw_text=raw_text)


def test_bitcoin_signal_from_a_gold_source_is_refused() -> None:
    """The exact production message that became a gold signal at a Bitcoin price."""
    result = _apply("BTCUSD SELL 79794.4 SL: 80994.4 TP: 76194.4 --Trade by William")

    assert result.action == "skip"
    assert result.reason == "foreign_instrument"


@pytest.mark.parametrize(
    "raw_text",
    [
        "USOIL SELL 61.20 SL: 62.00 TP: 59.80",
        "NAS100 BUY 20150 SL: 20050 TP: 20400",
        "EURUSD SELL 1.0925 SL: 1.0960 TP: 1.0860",
        "XAGUSD BUY 31.20 SL: 30.80 TP: 32.00",
        "BUY BTC NOW 79690",
    ],
)
def test_other_instruments_are_refused_for_a_gold_source(raw_text: str) -> None:
    assert _apply(raw_text).reason == "foreign_instrument"


def test_a_named_gold_trade_still_executes() -> None:
    """The guard must not touch the ordinary case."""
    result = _apply(
        "XAUUSD SELL 4292 SL: 4296 TP: 4280",
        entry_low="4292",
        entry_high="4292",
        stop_loss="4296",
        take_profits=["4280"],
    )

    assert result.action == "execute"
    assert result.reason != "foreign_instrument"


def test_profile_still_supplies_gold_when_no_instrument_is_named() -> None:
    """TGC's terse style relies on this and must keep working."""
    result = _apply(
        "Im selling 4412 Sl 4422",
        entry_low="4412",
        entry_high="4412",
        stop_loss="4422",
        take_profits=["4400"],
    )

    assert result.reason not in {"foreign_instrument", "missing_instrument"}


def test_a_message_naming_gold_and_another_market_is_still_treated_as_gold() -> None:
    """Only a message with no gold token at all can be a foreign instrument."""
    result = _apply(
        "GOLD SELL 4292 SL 4296 TP 4280 (BTC still running separately)",
        entry_low="4292",
        entry_high="4292",
        stop_loss="4296",
        take_profits=["4280"],
    )

    assert result.reason != "foreign_instrument"


def test_foreign_instrument_is_refused_even_without_a_source_profile() -> None:
    result = _apply(
        "BTCUSD SELL 79794.4 SL: 80994.4 TP: 76194.4",
        source_profile=None,
    )

    assert result.reason == "foreign_instrument"
