from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

from app.provider_research import analyze_provider_sample, duplicate_similarity
from app.telegram_source_gateway import (
    TelegramResearchDialog,
    TelegramResearchMessage,
    TelethonTelegramSourceGateway,
)


def _message(index: int, text: str, *, minutes: int = 0, edited: bool = False) -> TelegramResearchMessage:
    posted = datetime(2026, 8, 31, 6, 0, tzinfo=UTC) + timedelta(minutes=minutes)
    return TelegramResearchMessage(
        telegram_message_id=index,
        raw_text=text,
        posted_at=posted,
        edited_at=posted + timedelta(seconds=30) if edited else None,
    )


def _dialog(title: str, texts: list[str]) -> TelegramResearchDialog:
    return TelegramResearchDialog(
        chat_id=-1001234567890,
        title=title,
        kind="channel",
        messages=tuple(_message(i + 1, text, minutes=i * 30) for i, text in enumerate(texts)),
    )


def test_structured_gold_channel_is_candidate_and_ready() -> None:
    sample = analyze_provider_sample(
        _dialog(
            "Gold Precision Signals",
            [
                "BUY GOLD 3500-3498 SL 3492 TP1 3505 TP2 3510",
                "TP1 HIT move SL to BE",
                "SELL XAUUSD 3520 SL 3527 TP1 3515 TP2 3510",
                "close half and leave runner",
            ],
        )
    )
    assert sample.candidate is True
    assert sample.signal_like_messages == 2
    assert sample.structured_signal_messages == 2
    assert sample.management_messages >= 2
    assert sample.interpretation_readiness > 0.5


def test_scalper_is_labelled_not_rejected() -> None:
    sample = analyze_provider_sample(
        _dialog(
            "XAUUSD Scalping Signals",
            [
                f"BUY GOLD {3500 + i} SL {3490 + i} TP1 {3504 + i} TP2 {3507 + i}"
                for i in range(8)
            ],
        )
    )
    assert sample.candidate is True
    assert sample.style == "scalper"


def test_generic_forex_chat_without_signals_is_not_candidate() -> None:
    sample = analyze_provider_sample(
        _dialog(
            "Forex Friends",
            [
                "Morning everyone, what do you think about EURUSD?",
                "Interesting week ahead for markets.",
                "Does anyone trade gold?",
            ],
        )
    )
    assert sample.candidate is False
    assert sample.signal_like_messages == 0


def test_exact_signal_copying_is_flagged_as_duplicate_evidence() -> None:
    texts = [
        "BUY GOLD 3500 SL 3490 TP1 3510 TP2 3520",
        "SELL GOLD 3540 SL 3550 TP1 3530 TP2 3520",
        "BUY XAUUSD 3480 SL 3470 TP1 3490 TP2 3500",
        "SELL XAUUSD 3560 SL 3570 TP1 3550 TP2 3540",
    ]
    left = analyze_provider_sample(_dialog("Gold Signals One", texts))
    right = analyze_provider_sample(_dialog("Gold Signals Copy", texts))
    assert duplicate_similarity(left, right) == 1.0


def test_one_matching_signal_is_not_enough_to_call_duplicate() -> None:
    left = analyze_provider_sample(
        _dialog("Gold Signals One", ["BUY GOLD 3500 SL 3490 TP1 3510"])
    )
    right = analyze_provider_sample(
        _dialog("Gold Signals Two", ["BUY GOLD 3500 SL 3490 TP1 3510"])
    )
    assert duplicate_similarity(left, right) == 0.0


def test_research_gateway_contains_no_join_mutation() -> None:
    source = inspect.getsource(TelethonTelegramSourceGateway)
    assert "JoinChannelRequest" not in source
    assert "LeaveChannelRequest" not in source
    assert "scan_joined_research_dialogs" in source
