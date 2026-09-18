"""AIDY's first real reasoning call: one signal, read by a model, not just counted.

The Decision Ledger (aidy_decision_engine.py) is deliberately deterministic and says so:
duplicate/conflict checks and a provider's own recorded track record are exactly
computable, so v1 shipped without a model call. A provider with fewer than 20 resolved
trades gets `approve` + `insufficient_track_record_evidence` -- correct, but AIDY has
nothing to say about *that specific signal's* geometry, since track record alone cannot.

This is additive only. It never changes `aidy_decisions.decision_class` and it earns no
more authority than the deterministic engine already has -- the annotation is written
after the deterministic decision exists and is read by nothing in the execution path.
A failed, malformed or out-of-schema model response is never persisted as a guess; the
signal is simply left unannotated and picked up on a later pass once the caller
retries -- silence, never a manufactured lean.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from typing import Any

import httpx

MODEL_VERSION = "aidy_reasoning_engine_v2"
PROMPT_VERSION = "aidy_reasoning_prompt_v2"

# gpt-5-mini matches the existing message-interpretation supervisor's default model --
# the cheapest tier that still reasons, since this runs continuously and the owner's
# mandate caps new recurring spend rather than leaving it open-ended.
_DEFAULT_MODEL = "gpt-5-mini-2025-08-07"

# Priced per the model's published per-token rate at build time. Deliberately a rough,
# conservative (slightly high) estimate: this feeds a spend budget gate, so overstating
# cost fails safe (stops calls sooner) rather than understating it (keeps calling past
# the owner's real limit).
_USD_PER_INPUT_TOKEN = Decimal("0.00000025")
_USD_PER_OUTPUT_TOKEN = Decimal("0.000002")

REASONING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "lean": {"type": "string", "enum": ["agree", "caution", "disagree"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "rationale": {"type": "string"},
        "key_factors": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
    },
    "required": ["lean", "confidence", "rationale", "key_factors"],
}

_SYSTEM_INSTRUCTIONS = """You are AIDY, reasoning about one Gold (XAUUSD) trade signal that a
deterministic rule engine already approved -- either because the posting provider's own
recorded track record (win rate, average P&L across its resolved trades so far) cleared the
bar, or because it has too little history yet for that to mean anything. You are not deciding
whether to trade it -- that decision already happened and does not change, and a good overall
track record does not excuse a specific bad signal. Your only job is to read the signal's own
numeric geometry (entry, stop loss, take profits) and say whether it looks like a coherent,
disciplined setup or a careless one: is the stop distance sane relative to entry, is the
reward-to-risk ratio reasonable, are the take profits ordered and plausible, does anything look
internally inconsistent (e.g. a stop on the wrong side of entry for the stated direction).

You may also be given provider_fingerprint: a descriptive summary of this specific provider's
own historical wins versus losses (stop distance, reward:risk, which side and session actually
works for them), computed from their own resolved trade history. It is descriptive, not a
statistically certified rule -- treat it as background on how this provider tends to operate,
and let it inform your read of THIS signal (e.g. a stop far tighter than their own winning
trades typically use, or a side/session that has historically been weak for them, is worth
naming in key_factors) without ever treating it as proof this specific signal will win or lose.
When no fingerprint is given, or it says evidence is still too thin, reason from the signal's
own geometry alone as before.

You may also be given market_context: an objective, point-in-time snapshot of gold (XAUUSD)
conditions as of when this signal actually posted -- never anything known after the fact.
trend_by_timeframe gives each of M15/H1/H4's own directional read (bullish/bearish/flat/
unknown); trend_structure is the combined label across those (bullish_trend, bearish_trend,
range, mixed, or unknown when too few timeframes agree or are known). session is the trading
session gold was in. volatility_band and event_timing describe current volatility and whether
a scheduled event sits close to this moment, when known -- "unknown" or "blocked" here is a
genuine gap in evidence, not a signal of calm, and must be named as unknown rather than treated
as calm. quote_freshness/quote_state describe how trustworthy this snapshot itself is -- treat
a stale or unknown quote as a reason for lower confidence, not as evidence either way. When
market_context is given, you may note whether this signal's own direction (side) runs with or
against the current multi-timeframe trend, and whether the session or a nearby event timing
adds real risk to holding it -- but this is supporting context for your read of the signal's
geometry, never a replacement for it, and never a forecast of your own about where price goes
next. When market_context is absent, or its fields are unknown, reason from the signal's own
geometry and the provider fingerprint alone, exactly as before -- do not guess at conditions
you were not given.

Never invent facts not present in the signal, the fingerprint, or market_context.
lean=agree means the geometry looks sane and disciplined. lean=caution means it is workable
but has a real flaw worth noting -- including a signal that fights a clear, multi-timeframe-
confirmed trend, when market_context makes that visible. lean=disagree means the geometry
itself looks broken or reckless (e.g. stop wrong side of entry, reward:risk far worse than
1:1, targets not ordered in the trade's favour). confidence reflects how sure you are in that
overall read, not in whether the trade will win. rationale is one or two sentences. key_factors
is a short list of the specific observations that drove the lean (at most 5, each under 80
characters), numeric or market-context based."""


@dataclass(frozen=True, slots=True)
class SignalContext:
    decision_id: str
    provider_name: str
    side: str
    symbol: str
    entry_low: str | None
    entry_high: str | None
    stop_loss: str | None
    take_profits: list[str]
    decision_class: str
    decision_reasons: list[dict[str, Any]]
    trades_resolved: int
    provider_fingerprint_summary: str | None = None
    market_context: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ReasoningAnnotation:
    lean: str
    confidence: float
    rationale: str
    key_factors: list[str]
    model_name: str
    response_id: str | None
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: Decimal
    latency_ms: int


class AidyReasoningUnavailable(RuntimeError):
    """The model call failed or returned something outside the strict schema."""


def _prompt_payload(context: SignalContext) -> dict[str, Any]:
    return {
        "provider_name": context.provider_name,
        "provider_trades_resolved": context.trades_resolved,
        "side": context.side,
        "symbol": context.symbol,
        "entry_low": context.entry_low,
        "entry_high": context.entry_high,
        "stop_loss": context.stop_loss,
        "take_profits": context.take_profits,
        "deterministic_decision_class": context.decision_class,
        "deterministic_decision_reasons": context.decision_reasons,
        "provider_fingerprint": context.provider_fingerprint_summary,
        "market_context": context.market_context,
    }


class AidyReasoningEngine:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = _DEFAULT_MODEL,
        timeout_seconds: int = 15,
        base_url: str = "https://api.openai.com/v1",
    ) -> None:
        if not api_key:
            raise ValueError("openai_api_key_missing")
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._base_url = base_url.rstrip("/")

    def reason(self, context: SignalContext) -> ReasoningAnnotation:
        started = time.perf_counter()
        payload = {
            "model": self._model,
            "store": False,
            "reasoning": {"effort": "minimal"},
            "max_output_tokens": 600,
            "instructions": _SYSTEM_INSTRUCTIONS,
            "input": json.dumps(_prompt_payload(context), ensure_ascii=False),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "aidy_signal_reasoning",
                    "strict": True,
                    "schema": REASONING_SCHEMA,
                }
            },
        }
        try:
            response = httpx.post(
                f"{self._base_url}/responses",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            body = response.json()
            parsed = json.loads(self._output_text(body))
        except (httpx.HTTPError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise AidyReasoningUnavailable("aidy_reasoning_unavailable") from exc

        lean = str(parsed["lean"])
        if lean not in {"agree", "caution", "disagree"}:
            raise AidyReasoningUnavailable("aidy_reasoning_lean_invalid")
        confidence = float(parsed["confidence"])
        if not 0.0 <= confidence <= 1.0:
            raise AidyReasoningUnavailable("aidy_reasoning_confidence_invalid")

        usage = body.get("usage") or {}
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        estimated_cost = (
            Decimal(input_tokens) * _USD_PER_INPUT_TOKEN
            + Decimal(output_tokens) * _USD_PER_OUTPUT_TOKEN
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        return ReasoningAnnotation(
            lean=lean,
            confidence=confidence,
            rationale=str(parsed["rationale"])[:2000],
            key_factors=[str(factor)[:200] for factor in parsed["key_factors"]][:5],
            model_name=self._model,
            response_id=(str(body.get("id")) if body.get("id") else None),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=estimated_cost,
            latency_ms=latency_ms,
        )

    @staticmethod
    def _output_text(body: dict[str, Any]) -> str:
        for item in body.get("output", []):
            if item.get("type") != "message":
                continue
            for content in item.get("content", []):
                if content.get("type") == "output_text" and content.get("text"):
                    return str(content["text"])
        raise AidyReasoningUnavailable("aidy_reasoning_output_missing")


def prompt_digest(context: SignalContext) -> str:
    encoded = json.dumps(_prompt_payload(context), sort_keys=True, separators=(",", ":")).encode()
    return sha256(encoded).hexdigest()


__all__ = [
    "MODEL_VERSION",
    "PROMPT_VERSION",
    "REASONING_SCHEMA",
    "AidyReasoningEngine",
    "AidyReasoningUnavailable",
    "ReasoningAnnotation",
    "SignalContext",
    "prompt_digest",
]
