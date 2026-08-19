from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from app.ai_message_pipeline_canonical import CanonicalAiMessagePipeline, explicit_management_without_ai
from app.ai_message_supervisor import AiMessageDecision
from app.canonical_signal_ledger import CanonicalSignalLedger
from app.mt5_execution_day26 import Day26ExecutionError, _SignalInput
from app.paper_fresh_start_execution import PaperFreshStartExecutionService
import app.v1_message_policy as v1


def _decision(
    *,
    side: str = "BUY",
    entry_low=None,
    entry_high=None,
    stop_loss: str = "4380",
    take_profits: list[str] | None = None,
    order_type: str = "market",
) -> AiMessageDecision:
    return AiMessageDecision(
        decision="new_trade",
        action="execute",
        confidence=0.95,
        reason="model",
        extracted={
            "symbol": "XAUUSD",
            "side": side,
            "order_type": order_type,
            "entry_low": entry_low,
            "entry_high": entry_high,
            "stop_loss": stop_loss,
            "take_profits": take_profits or ["4395", "4400"],
            "double_lot": False,
            "update_type": None,
            "update_target": None,
            "update_value": None,
            "provider_claimed_pips": None,
        },
        model="test",
        response_id=None,
        latency_ms=1,
        source="openai",
        raw_text_sha256="x",
    )


def test_complete_market_buy_without_entry_executes_at_live_price() -> None:
    raw = "BUY GOLD NOW\nSL 4380\nTP 4395\nTP 4400"
    result = v1.apply_v1_message_policy(_decision(), raw_text=raw)
    assert result.decision == "new_trade"
    assert result.action == "execute"
    assert result.reason == "v1_complete_market_signal_live_entry"
    assert result.extracted["entry_low"] is None
    assert result.extracted["entry_high"] is None
    assert result.extracted["order_type"] == "market"


def test_complete_market_sell_without_entry_executes_at_live_price() -> None:
    raw = "SELL XAUUSD NOW\nSL 4420\nTP 4400\nTP 4390"
    result = v1.apply_v1_message_policy(
        _decision(
            side="SELL",
            stop_loss="4420",
            take_profits=["4400", "4390"],
        ),
        raw_text=raw,
    )
    assert result.action == "execute"
    assert result.extracted["entry_low"] is None
    assert result.extracted["entry_high"] is None


def test_no_entry_rule_never_converts_explicit_pending_to_market() -> None:
    raw = "BUY LIMIT GOLD 4388\nSL 4380\nTP 4395"
    result = v1.apply_v1_message_policy(
        _decision(order_type="pending"),
        raw_text=raw,
    )
    assert result.action == "skip"


def test_canonical_signal_accepts_nullable_market_entry() -> None:
    trade = CanonicalSignalLedger._parse_extracted(
        {
            "symbol": "XAUUSD",
            "side": "BUY",
            "order_type": "market",
            "entry_low": None,
            "entry_high": None,
            "stop_loss": "4380",
            "take_profits": ["4395", "4400"],
            "double_lot": False,
        }
    )
    assert trade.entry_low is None
    assert trade.entry_high is None
    assert trade.stop_loss == Decimal("4380")


def _service() -> PaperFreshStartExecutionService:
    service = object.__new__(PaperFreshStartExecutionService)
    service._paper_max_signal_age_seconds = 90.0
    return service


def _signal(*, side: str, stop: str, targets: tuple[str, ...]) -> _SignalInput:
    return _SignalInput(
        signal_id=uuid4(),
        symbol="XAUUSD",
        side=side,
        entry_low=Decimal("0"),
        entry_high=Decimal("0"),
        stop_loss=Decimal(stop),
        take_profits=tuple(Decimal(value) for value in targets),
        has_open_runner=False,
        signal_requests_double_lot=False,
        source_revision_index=0,
        source_posted_at=datetime.now(UTC),
    )


def test_live_execution_uses_ask_for_buy_when_provider_has_no_entry() -> None:
    service = _service()
    signal = _signal(side="BUY", stop="4380", targets=("4395", "4400"))
    state = SimpleNamespace(
        execution_ready=True,
        execution_block_reason=None,
        price=SimpleNamespace(ask=Decimal("4390"), bid=Decimal("4389.5")),
    )
    entry, returned_state = asyncio.run(
        service._resolve_entry(
            owner_user_id=uuid4(),
            signal=signal,
            day23=SimpleNamespace(),
            initial_state=state,
        )
    )
    assert entry == Decimal("4390")
    assert returned_state is state


def test_live_execution_uses_bid_for_sell_when_provider_has_no_entry() -> None:
    service = _service()
    signal = _signal(side="SELL", stop="4420", targets=("4380", "4370"))
    state = SimpleNamespace(
        execution_ready=True,
        execution_block_reason=None,
        price=SimpleNamespace(ask=Decimal("4390.5"), bid=Decimal("4390")),
    )
    entry, _ = asyncio.run(
        service._resolve_entry(
            owner_user_id=uuid4(),
            signal=signal,
            day23=SimpleNamespace(),
            initial_state=state,
        )
    )
    assert entry == Decimal("4390")


def test_live_price_must_still_be_inside_sl_tp_geometry() -> None:
    service = _service()
    signal = _signal(side="BUY", stop="4380", targets=("4390",))
    state = SimpleNamespace(
        execution_ready=True,
        execution_block_reason=None,
        price=SimpleNamespace(ask=Decimal("4392"), bid=Decimal("4391.5")),
    )
    try:
        asyncio.run(
            service._resolve_entry(
                owner_user_id=uuid4(),
                signal=signal,
                day23=SimpleNamespace(),
                initial_state=state,
            )
        )
    except Day26ExecutionError as exc:
        assert exc.code == "strict_directional_validation_failed"
    else:
        raise AssertionError("live price beyond TP must fail closed")


def test_gtmo_place_sl_is_literal_management_without_ai() -> None:
    result = explicit_management_without_ai(
        "actually lets place the SL at 4385 ok below this low."
    )
    assert result is not None
    assert result.action == "apply_update"
    assert result.source == "deterministic_no_ai"
    assert result.extracted["management_actions"] == [
        {"type": "edit_stop_loss", "target": "all", "value": "4385"}
    ]


def test_plain_gtmo_zone_is_not_rejected_when_ai_guesses_pending() -> None:
    raw = (
        "Gold buy now 4390.9 - 4388\n\n"
        "SL: 4386\n\n"
        "TP: 4394\nTP: 4396\nTP: 4398\nTP: open"
    )
    result = v1.apply_v1_message_policy(
        _decision(
            entry_low="4388",
            entry_high="4390.9",
            stop_loss="4386",
            take_profits=["4394", "4396", "4398"],
            order_type="pending",
        ),
        raw_text=raw,
    )
    assert result.decision == "new_trade"
    assert result.action == "execute"
    assert result.reason == "v1_complete_zone_signal"
    assert result.extracted["order_type"] == "market"


def test_explicit_management_decision_is_owned_by_canonical_pipeline() -> None:
    pipeline = object.__new__(CanonicalAiMessagePipeline)
    result = pipeline._decide(
        source_id=uuid4(),
        telegram_message_id=1,
        revision_index=0,
        raw_text="place the SL at 4385",
        source_status="testing",
        reply_context=None,
        previous_text=None,
    )
    assert result.action == "apply_update"
    assert result.source == "deterministic_no_ai"
