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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from typing import Any

import httpx

from app.aidy_evidence_contract import (
    EvidenceClaimValidationError,
    assert_no_freeform_provider_history,
    validate_provider_claim_refs,
)

MODEL_VERSION = "aidy_reasoning_engine_v5"
PROMPT_VERSION = "aidy_reasoning_prompt_v7"

# Bounded on purpose: each round trip is a real OpenAI request, so this caps both cost and
# how long one signal can take to reason about, not just how many timeframes/hours it may
# ask for in one call (each tool's own parameters already cap that per call).
CANDLE_TOOL_NAME = "get_recent_candles"
CALENDAR_TOOL_NAME = "get_economic_calendar"
_MAX_TOOL_ROUNDS = 2

CANDLE_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": CANDLE_TOOL_NAME,
    "description": (
        "Fetch real, point-in-time gold (XAUUSD) OHLC candles aggregated to a requested "
        "timeframe, ending at (never after) this signal's own posted time. Use this when "
        "market_context's single trend label per timeframe is not enough to judge this "
        "signal's entry against recent price structure -- e.g. whether the entry sits inside "
        "a recent range, at a recent high/low, or against a clear short-term swing."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "timeframe_minutes": {
                "type": "integer",
                "enum": [1, 5, 15, 30, 45, 60],
                "description": "Candle size in minutes.",
            },
            "lookback_count": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "description": "How many of the most recent candles at that timeframe to return.",
            },
        },
        "required": ["timeframe_minutes", "lookback_count"],
    },
}

CALENDAR_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": CALENDAR_TOOL_NAME,
    "description": (
        "Fetch real scheduled macro economic events (e.g. NFP, CPI, Fed rate decisions) "
        "around this signal's own posted time. Returns each event's title, country, impact "
        "(low/medium/high), scheduled time, published consensus forecast and prior reading -- "
        "never a realized/actual outcome, since that is only ever knowable once the event has "
        "genuinely happened. Use this to check whether a high-impact USD event sits close "
        "enough to this signal's own timing to add real risk to holding it, especially when "
        "market_context's event_timing field is unknown."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "hours_before": {
                "type": "integer",
                "minimum": 0,
                "maximum": 72,
                "description": "How many hours before this signal's own posted time to include.",
            },
            "hours_after": {
                "type": "integer",
                "minimum": 0,
                "maximum": 72,
                "description": "How many hours after this signal's own posted time to include.",
            },
            "min_impact": {
                "type": "string",
                "enum": ["low", "medium", "high"],
                "description": "Only return events at or above this impact level.",
            },
        },
        "required": ["hours_before", "hours_after", "min_impact"],
    },
}

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
        "shadow_action": {
            "type": "string",
            "enum": ["take", "reduce", "hold", "reject", "need_more_evidence"],
        },
        "risk_multiplier": {"type": "number", "minimum": 0, "maximum": 1},
        "action_reason": {"type": "string"},
        "provider_claim_refs": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 8,
        },
    },
    "required": [
        "lean",
        "confidence",
        "rationale",
        "key_factors",
        "shadow_action",
        "risk_multiplier",
        "action_reason",
        "provider_claim_refs",
    ],
}

_SYSTEM_INSTRUCTIONS = """You are AIDY, reasoning about one Gold (XAUUSD) trade signal that a
deterministic rule engine already approved -- either because the posting provider's own
recorded track record (win rate, average P&L across its resolved trades so far) cleared the
bar, or because it has too little history yet for that to mean anything. The production
decision already happened and your output cannot change it. Your job is to independently assess
this specific signal and produce a shadow-only final action for counterfactual testing; a good
overall track record does not excuse a specific bad signal. Start with the signal's own
numeric geometry (entry, stop loss, take profits) and decide whether it looks like a coherent,
disciplined setup or a careless one: is the stop distance sane relative to entry, is the
reward-to-risk ratio reasonable, are the take profits ordered and plausible, does anything look
internally inconsistent (e.g. a stop on the wrong side of entry for the stated direction).

You may also be given provider_evidence_claims. These are the ONLY provider-history facts you
may use. Each is an atomic point-in-time fact with an exact id, source path, value, sample size,
version and evidence timestamp. The application pre-scopes side/session performance evidence
to THIS signal's side and session, so an opposite-side or different-session cohort is deliberately
not available unless there is an explicit comparison claim that passed its own sample gate.
If provider history materially affects your action, put the exact supporting claim id(s) in
provider_claim_refs. Never infer a missing cohort from absence. A behaviour claim such as
dominant_session_utc describes posting/communication behaviour, NOT profitability in that
session, and must never be converted into a performance claim.

Provider-history facts must NOT be restated in rationale, key_factors or action_reason. Those
free-text fields are restricted to the current signal's geometry, current market evidence,
current/recent message semantics and tool evidence. Provider history is represented only by
provider_claim_refs, which are validated and persisted alongside the exact evidence snapshot.
If no provider evidence is relevant, use an empty provider_claim_refs array.

You may also be given market_context: an objective, point-in-time snapshot of gold (XAUUSD)
conditions as of when this signal actually posted -- never anything known after the fact.
trend_by_timeframe gives each of M15/H1/H4's own directional read (bullish/bearish/flat/
unknown); trend_structure is the combined label across those (bullish_trend, bearish_trend,
range, mixed, or unknown when too few timeframes agree or are known). session is the trading
session gold was in. volatility_band and event_timing describe current volatility and whether
a scheduled event sits close to this moment, when known -- "unknown" or "blocked" here is a
genuine gap in evidence, not a signal of calm, and must be named as unknown rather than treated
as calm. quote_freshness/quote_state describe how trustworthy this snapshot itself is -- treat
a stale or unknown quote as a reason for lower confidence, not as evidence either way.

market_context may also include todays_scheduled_events: every medium/high-impact macro event
scheduled anywhere in this signal's own UTC calendar day, each with its time, session, country,
impact, published forecast and prior reading -- never a realized outcome, since that could not
be known yet. This is a standing map of the day, not something you asked for, so use it freely:
note when a nearby event sits close enough to this signal's own timing to add real holding
risk, independent of whether you also call get_economic_calendar for a narrower or wider query.

market_context may also include gold_state, a versioned point-in-time Gold-state dossier from
the standalone AIDY Gold brain. Use a surface ONLY when its own decision_input_allowed field is
true. price_liquidity contains measured session ranges, prior-period structure, breakout/
reversion state, wick/swing structure and feed health. volatility contains realised-volatility
and jump/continuity evidence when available. Research surfaces explicitly marked
decision_input_allowed=false are NOT evidence for this decision; their presence documents an
unknown/unqualified research gap. gold_state is descriptive context, not a directional edge
claim. Never convert an UNKNOWN or research-only field into a bullish/bearish conclusion.

When market_context is given, you may note whether this signal's own direction (side) runs with or
against the current multi-timeframe trend, and whether the session or a nearby event timing
adds real risk to holding it -- but this is supporting context for your read of the signal's
geometry, never a replacement for it, and never a forecast of your own about where price goes
next. When market_context is absent, or its fields are unknown, reason from the signal's own
geometry and whatever validated provider_evidence_claims exist -- do not guess at conditions
or provider behaviour you were not given.

You may also be offered a get_recent_candles tool: real OHLC candles at a timeframe and
lookback you choose, ending at (never after) this signal's own posted time. Use it when the
trade's quality depends on price structure that a single trend label cannot establish. In
particular, fetch candles when trend_structure is mixed/range/unknown, when the signal runs
against a clear multi-timeframe trend, or when entry/stop/targets need recent swing/range
context to decide whether their geometry is actually sensible. Do not call it merely to
confirm an already-clear, trend-aligned setup. If you call it, the candles you get back are
real market data, never a forecast -- describe what they show, never what you expect to happen
next.

You may also be offered a get_economic_calendar tool: real scheduled macro events (e.g. NFP,
CPI, Fed decisions) with published forecast and prior reading, around this signal's own posted
time -- never a realized/actual outcome, since that could not be known yet. Call it when
market_context's event_timing is unknown or you want to check whether a specific high-impact
USD event sits close enough to this signal to add real holding risk. A forecast is a public
consensus number known in advance, not a prediction of your own -- report it as such.

You may also be given recent_messages and self_calibration. recent_messages are strictly
messages posted no later than this signal, so use sequence/context but never import a price or
instruction from an older message unless the current signal or direct reply semantics actually
make that linkage legitimate. self_calibration describes your own earlier resolved reasoning
performance and may lower confidence, but it is not permission to invent a provider-history
fact. Provider historical behaviour/performance remains usable only through provider_claim_refs.

supplemental_evidence may contain point-in-time candle/calendar evidence fetched before the model
call. Treat it exactly like a successful tool result: real evidence available at this signal's
timestamp, not hindsight. If evidence is missing or marked unavailable, lower confidence rather
than guessing.

event_liquidity_execution_context is deterministic, point-in-time factual evidence built by the
application from the signal geometry, immutable quote/liquidity context, scheduled-event metadata,
and broker execution calibration available no later than the signal. Use it to identify factual
execution problems such as price already being outside the entry zone, targets already crossed,
stale/invalid quote evidence, nearby scheduled-event risk, or historically observed slippage/cash
friction when calibration status is engineering_calibrated. Scheduled events are risk/timing facts,
never directional predictions, and no realized event outcome is available. If execution calibration
is insufficient or marked invalid, treat execution friction as UNKNOWN rather than filling the gap.

In addition to lean, produce a SHADOW-ONLY final action. This action has no broker authority.
take means the valid signal would be accepted at normal configured risk; reduce means it would be
accepted at smaller risk and risk_multiplier must be between 0 and 1; hold means wait rather than
enter immediately; reject means the setup should not be taken; need_more_evidence means the
available evidence is too incomplete to make a responsible call. For take use risk_multiplier=1,
and for hold/reject/need_more_evidence use risk_multiplier=0. Do not use provider reputation alone
to reject a coherent signal, and do not override malformed geometry or missing evidence merely
because a provider has historically performed well.

Never invent facts not present in the signal, validated provider_evidence_claims, recent
messages, self calibration, market_context, supplemental evidence, or any candles/calendar
events you fetched.
lean=agree means the geometry looks sane and disciplined. lean=caution means it is workable
but has a real flaw worth noting -- including a signal that fights a clear, multi-timeframe-
confirmed trend, when market_context makes that visible. lean=disagree means the geometry
itself looks broken or reckless (e.g. stop wrong side of entry, reward:risk far worse than
1:1, targets not ordered in the trade's favour). confidence reflects how sure you are in that
overall read, not in whether the trade will win. rationale is one or two sentences. key_factors
is a short list of the specific current-signal/current-market observations that drove the
lean (at most 5, each under 80 characters). Do not put provider-history facts in free text;
represent them only through provider_claim_refs."""


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
    provider_evidence_claims: list[dict[str, Any]] | None = None
    market_context: dict[str, Any] | None = None
    provider_intelligence: dict[str, Any] | None = None
    provider_profile: dict[str, Any] | None = None
    recent_messages: list[dict[str, Any]] | None = None
    self_calibration: dict[str, Any] | None = None
    supplemental_evidence: dict[str, Any] | None = None
    event_liquidity_execution_context: dict[str, Any] | None = None
    preflight_evidence_calls: int = 0


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
    request_count: int = 1
    tool_calls_made: int = 0
    preflight_evidence_calls: int = 0
    shadow_action: str = "need_more_evidence"
    risk_multiplier: float = 0.0
    action_reason: str = ""
    provider_claim_refs: tuple[str, ...] = ()


ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class AidyReasoningUnavailable(RuntimeError):
    """The model call failed or returned something outside the strict schema."""


def _prompt_payload(context: SignalContext) -> dict[str, Any]:
    return {
        "side": context.side,
        "symbol": context.symbol,
        "entry_low": context.entry_low,
        "entry_high": context.entry_high,
        "stop_loss": context.stop_loss,
        "take_profits": context.take_profits,
        "deterministic_decision_class": context.decision_class,
        "deterministic_decision_reasons": context.decision_reasons,
        "provider_evidence_claims": context.provider_evidence_claims or [],
        "market_context": context.market_context,
        "recent_messages": context.recent_messages,
        "self_calibration": context.self_calibration,
        "supplemental_evidence": context.supplemental_evidence,
        "event_liquidity_execution_context": context.event_liquidity_execution_context,
    }


class AidyReasoningEngine:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = _DEFAULT_MODEL,
        timeout_seconds: int = 15,
        base_url: str = "https://api.openai.com/v1",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("openai_api_key_missing")
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._base_url = base_url.rstrip("/")
        # Test-only seam: production never passes this, so httpx.AsyncClient's real default
        # transport (a genuine HTTP connection) is used exactly as before.
        self._transport = transport

    async def reason(
        self,
        context: SignalContext,
        *,
        tool_executor: ToolExecutor | None = None,
        tool_schemas: list[dict[str, Any]] | None = None,
    ) -> ReasoningAnnotation:
        """Reason about one signal, optionally letting the model call one of tool_schemas.

        tool_schemas is which tools are actually available this call (the caller only ever
        configures the clients it has -- candles, calendar, both, or neither); tool_executor
        is a single dispatcher the caller routes to the right one by name. Offering a schema
        with no executor (or vice versa) is treated as no tools available at all, since
        either alone cannot honour a call the model makes.

        Bounded to _MAX_TOOL_ROUNDS rounds of tool use; the final round never offers tools,
        so the model cannot stall indefinitely -- it must return its structured answer with
        whatever it has fetched so far. Each round is one real OpenAI request; token usage
        and cost are summed across every round actually made, never just the last one.
        """
        started = time.perf_counter()
        input_items: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": json.dumps(_prompt_payload(context), ensure_ascii=False),
            }
        ]
        total_input_tokens = 0
        total_output_tokens = 0
        response_id: str | None = None
        request_count = 0
        tool_calls_made = 0
        body: dict[str, Any] | None = None

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds, transport=self._transport
            ) as client:
                have_tools = tool_executor is not None and bool(tool_schemas)
                for round_index in range(_MAX_TOOL_ROUNDS + 1):
                    offer_tools = have_tools and round_index < _MAX_TOOL_ROUNDS
                    payload: dict[str, Any] = {
                        "model": self._model,
                        "store": False,
                        "reasoning": {"effort": "minimal"},
                        "max_output_tokens": 700,
                        "instructions": _SYSTEM_INSTRUCTIONS,
                        "input": input_items,
                        "text": {
                            "format": {
                                "type": "json_schema",
                                "name": "aidy_signal_reasoning",
                                "strict": True,
                                "schema": REASONING_SCHEMA,
                            }
                        },
                    }
                    if offer_tools:
                        payload["tools"] = tool_schemas

                    response = await client.post(
                        f"{self._base_url}/responses",
                        headers={
                            "Authorization": f"Bearer {self._api_key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
                    response.raise_for_status()
                    body = response.json()
                    request_count += 1

                    usage = body.get("usage") or {}
                    total_input_tokens += int(usage.get("input_tokens") or 0)
                    total_output_tokens += int(usage.get("output_tokens") or 0)
                    response_id = (str(body.get("id")) if body.get("id") else response_id)

                    function_calls = [
                        item
                        for item in body.get("output", [])
                        if item.get("type") == "function_call"
                    ]
                    if not function_calls:
                        break
                    if tool_executor is None:
                        raise AidyReasoningUnavailable("aidy_reasoning_unexpected_tool_call")

                    input_items = [*input_items, *function_calls]
                    for call in function_calls:
                        tool_calls_made += 1
                        try:
                            arguments = json.loads(call.get("arguments") or "{}")
                        except json.JSONDecodeError:
                            arguments = {}
                        result = await tool_executor(str(call.get("name")), arguments)
                        input_items.append(
                            {
                                "type": "function_call_output",
                                "call_id": call.get("call_id"),
                                "output": json.dumps(result, ensure_ascii=False),
                            }
                        )
                else:
                    raise AidyReasoningUnavailable("aidy_reasoning_tool_round_limit_exceeded")

            assert body is not None  # the loop always runs at least once
            parsed = json.loads(self._output_text(body))
        except (httpx.HTTPError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise AidyReasoningUnavailable("aidy_reasoning_unavailable") from exc

        lean = str(parsed["lean"])
        if lean not in {"agree", "caution", "disagree"}:
            raise AidyReasoningUnavailable("aidy_reasoning_lean_invalid")
        confidence = float(parsed["confidence"])
        if not 0.0 <= confidence <= 1.0:
            raise AidyReasoningUnavailable("aidy_reasoning_confidence_invalid")
        shadow_action = str(parsed["shadow_action"])
        if shadow_action not in {"take", "reduce", "hold", "reject", "need_more_evidence"}:
            raise AidyReasoningUnavailable("aidy_reasoning_shadow_action_invalid")
        risk_multiplier = float(parsed["risk_multiplier"])
        if not 0.0 <= risk_multiplier <= 1.0:
            raise AidyReasoningUnavailable("aidy_reasoning_risk_multiplier_invalid")
        if shadow_action == "take":
            risk_multiplier = 1.0
        elif shadow_action in {"hold", "reject", "need_more_evidence"}:
            risk_multiplier = 0.0
        elif shadow_action == "reduce" and not 0.0 < risk_multiplier < 1.0:
            raise AidyReasoningUnavailable("aidy_reasoning_reduce_multiplier_invalid")

        rationale = str(parsed["rationale"])[:2000]
        key_factors = [str(factor)[:200] for factor in parsed["key_factors"]][:5]
        action_reason = str(parsed["action_reason"])[:1000]
        try:
            provider_claim_refs = validate_provider_claim_refs(
                list(parsed["provider_claim_refs"]),
                context.provider_evidence_claims or [],
            )
            assert_no_freeform_provider_history(
                provider_name=context.provider_name,
                rationale=rationale,
                key_factors=key_factors,
                action_reason=action_reason,
            )
        except (EvidenceClaimValidationError, TypeError, ValueError) as exc:
            raise AidyReasoningUnavailable("aidy_reasoning_provider_claim_invalid") from exc

        estimated_cost = (
            Decimal(total_input_tokens) * _USD_PER_INPUT_TOKEN
            + Decimal(total_output_tokens) * _USD_PER_OUTPUT_TOKEN
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        return ReasoningAnnotation(
            lean=lean,
            confidence=confidence,
            rationale=rationale,
            key_factors=key_factors,
            model_name=self._model,
            response_id=response_id,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            estimated_cost_usd=estimated_cost,
            latency_ms=latency_ms,
            request_count=request_count,
            tool_calls_made=tool_calls_made,
            preflight_evidence_calls=context.preflight_evidence_calls,
            shadow_action=shadow_action,
            risk_multiplier=risk_multiplier,
            action_reason=action_reason,
            provider_claim_refs=provider_claim_refs,
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
