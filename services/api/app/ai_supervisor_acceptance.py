"""One-shot live acceptance probe for the OpenAI Telegram supervisor.

The probe calls only the interpretation gateway. It never writes a Signal, Position,
order, lifecycle event, or broker request. Enable it temporarily at deploy time with
SUPER_SIGNALS_AI_ACCEPTANCE_PROBE=1.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.ai_message_supervisor import OpenAiMessageSupervisor
from app.config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _ProbeCase:
    name: str
    raw_text: str
    decision: str
    action: str
    reason: str | None = None
    symbol: str | None = None
    side: str | None = None
    entry_low: str | None = None
    entry_high: str | None = None
    stop_loss: str | None = None
    take_profits: tuple[str, ...] | None = None
    double_lot: bool | None = None


_CASES = (
    _ProbeCase(
        name="exact_market_executes",
        raw_text="BUY GOLD @ 4371\nTP 4375\nTP 4380\nTP 4385\nSL 4360",
        decision="new_trade",
        action="execute",
        symbol="XAUUSD",
        side="BUY",
        entry_low="4371",
        entry_high="4371",
        stop_loss="4360",
        take_profits=("4375", "4380", "4385"),
        double_lot=False,
    ),
    _ProbeCase(
        name="range_trade_is_understood_but_skipped",
        raw_text=(
            "BUY GOLD @ 4396/4391\n\nTP 4399\nTP 4403\nTP 4408\n"
            "TP OPEN\nSL 4390\n\nHIGH RISK TRADE"
        ),
        decision="new_trade",
        action="skip",
        reason="unsupported_entry_range",
        symbol="XAUUSD",
        side="BUY",
        entry_low="4391",
        entry_high="4396",
        stop_loss="4390",
        take_profits=("4399", "4403", "4408"),
        double_lot=False,
    ),
    _ProbeCase(
        name="second_entry_is_understood_but_skipped",
        raw_text=(
            "🟢BUY XAUUSD\nENTRY: 4385\nSecond entry: 4380\n\nSL: 4371\n"
            "TP1: 4391\nTP2: 4396\nTP3: 4400\nTP4: open\n\nManage risk properly."
        ),
        decision="new_trade",
        action="skip",
        reason="unsupported_multiple_entries",
        symbol="XAUUSD",
        side="BUY",
        entry_low="4380",
        entry_high="4385",
        stop_loss="4371",
        take_profits=("4391", "4396", "4400"),
        double_lot=False,
    ),
    _ProbeCase(
        name="pending_order_is_understood_but_skipped",
        raw_text="BUY LIMIT GOLD @ 4387\nTP 4391\nTP 4396\nSL 4381",
        decision="new_trade",
        action="skip",
        reason="unsupported_pending_order",
        symbol="XAUUSD",
        side="BUY",
        entry_low="4387",
        entry_high="4387",
        stop_loss="4381",
        take_profits=("4391", "4396"),
        double_lot=False,
    ),
    _ProbeCase(
        name="open_target_is_understood_but_skipped",
        raw_text="BUY GOLD @ 4385\nTP 4391\nTP 4396\nTP OPEN\nSL 4371",
        decision="new_trade",
        action="skip",
        reason="unsupported_open_target",
        symbol="XAUUSD",
        side="BUY",
        entry_low="4385",
        entry_high="4385",
        stop_loss="4371",
        take_profits=("4391", "4396"),
        double_lot=False,
    ),
    _ProbeCase(
        name="preparation_is_ignored",
        raw_text="PREPARE FOR A BUY",
        decision="preparation",
        action="ignore",
    ),
    _ProbeCase(
        name="incomplete_trade_is_skipped",
        raw_text="Sell Gold Now",
        decision="non_actionable",
        action="skip",
        reason="provider_instruction_incomplete",
    ),
)


def run_ai_supervisor_acceptance_probe(settings: Settings) -> None:
    """Call OpenAI against safe fixtures and fail startup if the contract drifts."""
    if not settings.ai_supervisor_enabled:
        raise RuntimeError("AI acceptance probe requires the AI supervisor to be enabled")
    if not settings.ai_supervisor_api_key:
        raise RuntimeError("AI acceptance probe requires an OpenAI API key")

    supervisor = OpenAiMessageSupervisor(
        api_key=settings.ai_supervisor_api_key,
        model=settings.ai_supervisor_model,
        timeout_seconds=settings.ai_supervisor_timeout_seconds,
    )

    for case in _CASES:
        result = supervisor.decide(raw_text=case.raw_text, source_status="testing")
        errors: list[str] = []
        if result.source != "openai":
            errors.append(f"source={result.source}")
        if result.decision != case.decision:
            errors.append(f"decision={result.decision}")
        if result.action != case.action:
            errors.append(f"action={result.action}")
        if case.reason is not None and case.reason not in result.reason:
            errors.append(f"reason={result.reason}")

        expected_fields = {
            "symbol": case.symbol,
            "side": case.side,
            "entry_low": case.entry_low,
            "entry_high": case.entry_high,
            "stop_loss": case.stop_loss,
        }
        for field, expected in expected_fields.items():
            if expected is not None and str(result.extracted.get(field)) != expected:
                errors.append(f"{field}={result.extracted.get(field)}")
        if case.take_profits is not None:
            actual_tps = tuple(str(value) for value in result.extracted.get("take_profits", []))
            if actual_tps != case.take_profits:
                errors.append(f"take_profits={actual_tps}")
        if case.double_lot is not None and bool(result.extracted.get("double_lot")) != case.double_lot:
            errors.append(f"double_lot={result.extracted.get('double_lot')}")

        if errors:
            raise RuntimeError(
                f"AI acceptance probe failed case={case.name} " + " ".join(errors)
            )
        logger.info(
            "AI acceptance probe passed case=%s decision=%s action=%s reason=%s latency_ms=%s",
            case.name,
            result.decision,
            result.action,
            result.reason,
            result.latency_ms,
        )

    logger.info("AI acceptance probe PASSED all_cases=%s", len(_CASES))
