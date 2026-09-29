"""Cheap deterministic guard in front of the paid Telegram semantic supervisor.

The prefilter is intentionally conservative. It only handles content that cannot create
or manage a trade under the production contract: ordinary chatter, obvious marketing,
provider result summaries that the semantic supervisor historically ignored, and a
known source's standalone price pulses. Anything with plausible entry or management
intent is left to the normal semantic path.

The raw Telegram message remains stored in the ledger and therefore remains available
as bounded same-source context for later genuine trade messages. This module only avoids
paying for an OpenAI decision on the current non-actionable message.
"""

from __future__ import annotations

from hashlib import sha256
import re

from app.ai_message_supervisor import AiMessageDecision

_MODEL = "canonical-deterministic-ai-prefilter-v1"

# Broad on purpose: if a message contains one of these cues we prefer paying for AI over
# accidentally suppressing terse provider intent. The production pipeline already handles
# explicit deterministic management before this prefilter is reached.
_ACTION_CUE = re.compile(
    r"\b(?:"
    r"BUY(?:S|ING)?|SELL(?:S|ING)?|LONG|SHORT|"
    r"ENTRY|ENTER|SL|STOP(?:\s+LOSS)?|TP\s*#?\s*\d*|TAKE\s+PROFIT|TARGET|"
    r"CLOSE|CLOSING|EXIT|OUT|CANCEL|BREAKEVEN|BREAK\s*[- ]?EVEN|"
    r"MOVE|SECURE|HOLD|RUNNER|RISK\s*[- ]?FREE|PARTIAL|HALF|"
    r"LIMIT|PENDING|TAKE|SET|ADJUST|CHANGE|REMOVE|TRAIL|LOCK|CUT|BOOK|BANK"
    r")\b",
    re.IGNORECASE,
)

# Messages that are clearly reporting already-achieved results. Historically these were
# overwhelmingly classified as trade_update+ignore and never produced an execution or
# applied update. Exact HIT/close/BE instructions are intentionally not included here.
_RESULT_ONLY = re.compile(
    r"(?:"
    r"PROFITS?\s+FROM\s+LAYERING|"
    r"DAILY\s+PERFORMANCE|"
    r"TOTAL\s+PIPS(?:\s+GAINED)?|"
    r"FLOATING\s+IN\s+\+?\d+(?:\.\d+)?\s+PIPS(?:\s+PROFIT)?|"
    r"ALL\s+ENT(?:R|RI)IES\s+IN\s+PROFIT|"
    r"ALL['’]?S?\s+TARGETS?\s+DONE|"
    r"TP\s*#?\s*\d+\s*✅(?:\s*/\s*\d+(?:\.\d+)?\s*PIPS?)?"
    r")",
    re.IGNORECASE,
)

_MARKETING = re.compile(
    r"(?:"
    r"CLICK\s+HERE|JOIN\s+(?:MY\s+|THE\s+)?VIP|CLAIM\s+YOUR\s+SPOT|"
    r"UNLOCK\s+ALL\s+THE\s+TRADES|E-?BOOK|GIVEAWAY|MEMBER\s+FEEDBACK|"
    r"CONTACT\s+US|TEXT\s+ME\s*:\s*ACTIVE|FREE\s+CHANNEL|SUBSCRIBE"
    r")",
    re.IGNORECASE,
)

_PREPARATION = re.compile(
    r"(?:^|\b)(?:READY|GET\s+READY|WHO\s+IS\s+READY|ACTIVE|NEXT\s+ZONE|WATCH\s+THIS)(?:\b|$)",
    re.IGNORECASE,
)

_PURE_PRICE = re.compile(r"^\s*[+-]?\d{3,5}(?:\.\d+)?\s*[^A-Za-z0-9\s]*\s*$")
_GOLD_WITH_NUMBER = re.compile(r"\b(?:GOLD|XAUUSD)\b[\s\S]*\d|\d[\s\S]*\b(?:GOLD|XAUUSD)\b", re.IGNORECASE)
_OPTIONAL_RISK_FREE = re.compile(r"\bRISK\s*[- ]?FREE\s+IF\s+YOU\s+WANT\b", re.IGNORECASE)

# September evidence showed this source emitting hundreds of standalone numeric/rocket
# price pings and zero executable/apply-update outcomes from that format. Keep the rule
# source-specific rather than assuming all providers use naked numbers the same way.
_PRICE_PULSE_SOURCE = "gold vip (xauusd)"


def _ignore_decision(raw_text: str, *, decision: str, reason: str) -> AiMessageDecision:
    return AiMessageDecision(
        decision=decision,
        action="ignore",
        confidence=1.0,
        reason=reason,
        extracted={
            "symbol": None,
            "side": None,
            "order_type": None,
            "entry_low": None,
            "entry_high": None,
            "stop_loss": None,
            "take_profits": [],
            "double_lot": False,
            "update_type": None,
            "update_target": None,
            "update_value": None,
            "provider_claimed_pips": None,
        },
        model=_MODEL,
        response_id=None,
        latency_ms=0,
        source="deterministic_no_ai",
        raw_text_sha256=sha256((raw_text or "").encode("utf-8")).hexdigest(),
    )


def deterministic_ai_cost_prefilter(
    *,
    raw_text: str,
    source_name: str | None,
    reply_context: str | None,
    is_edit: bool,
) -> AiMessageDecision | None:
    """Return a free deterministic ignore decision, or ``None`` to keep semantic AI.

    Direct replies are always left to AI because otherwise harmless words such as "yes"
    can acquire lifecycle meaning from the replied-to trade. Generic edited messages are
    also left to AI; only unmistakable marketing/result noise is filtered on edits.
    """
    value = (raw_text or "").strip()
    if not value:
        return None
    if reply_context and reply_context.strip():
        return None

    # Preserve provider result semantics in the audit trail while avoiding a paid call.
    if _RESULT_ONLY.search(value):
        return _ignore_decision(
            raw_text,
            decision="trade_update",
            reason="deterministic_result_report_only_prefilter",
        )

    # Explicitly optional advice is not an instruction to alter a position.
    if _OPTIONAL_RISK_FREE.search(value):
        return _ignore_decision(
            raw_text,
            decision="trade_update",
            reason="deterministic_optional_management_prefilter",
        )

    # Marketing sometimes contains rhetorical BUY/SELL language. Do not pay the model to
    # rediscover that it is marketing unless the post also contains concrete trade fields.
    if _MARKETING.search(value) and not re.search(
        r"\b(?:SL|STOP\s+LOSS|TP\s*#?\s*\d+|ENTRY)\b",
        value,
        re.IGNORECASE,
    ):
        return _ignore_decision(
            raw_text,
            decision="chatter",
            reason="deterministic_marketing_prefilter",
        )

    # Source-specific price ticker noise. Other providers retain AI for naked numbers.
    if (
        (source_name or "").strip().casefold() == _PRICE_PULSE_SOURCE
        and _PURE_PRICE.fullmatch(value)
    ):
        return _ignore_decision(
            raw_text,
            decision="chatter",
            reason="deterministic_price_pulse_prefilter",
        )

    if is_edit:
        return None

    # A GOLD/XAUUSD message carrying a number can be a provider-specific terse entry even
    # when it lacks standard verbs. Keep those on the semantic path.
    if _GOLD_WITH_NUMBER.search(value):
        return None

    if _ACTION_CUE.search(value):
        return None

    if _PREPARATION.search(value):
        return _ignore_decision(
            raw_text,
            decision="preparation",
            reason="deterministic_preparation_prefilter",
        )

    return _ignore_decision(
        raw_text,
        decision="chatter",
        reason="deterministic_obvious_chatter_prefilter",
    )


__all__ = ["deterministic_ai_cost_prefilter"]
