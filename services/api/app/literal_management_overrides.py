"""Mechanical management grammar corrections for explicit provider instructions.

These are not AI inferences. They cover literal imperative/current-state phrases that
must reach the broker even when an AI classification calls the message chatter or a
provider result. The wrapper remains fail-closed for optional/future wording.
"""

from __future__ import annotations

import re
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import Any

from app.day27_management_policy import Day27ManagementPolicyResult

# Observed FXTradingVision forms include both singular and plural stoploss wording:
# "Move gold stoploss to 4375" and "Move all gold stoplosses to 4405".
_NUMERIC_STOP = re.compile(
    r"\b(?:MOVE|SET|CHANGE|UPDATE)\s+"
    r"(?:ALL\s+)?"
    r"(?:(?:THE|YOUR|MY|OUR)\s+)?"
    r"(?:(?:GOLD|XAUUSD)\s+)?"
    r"(?:SL(?:S)?|STOP\s*LOSS(?:ES)?)\s+"
    r"(?:BACK\s+)?(?:TO|AT)?\s*(\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)

_MOVE_TO_ENTRY = re.compile(
    r"\b(?:MOVE|SET|PUT)\s+"
    r"(?:ALL\s+)?"
    r"(?:(?:THE|YOUR|MY|OUR)\s+)?"
    r"(?:(?:GOLD|XAUUSD)\s+)?"
    r"(?:SL(?:S)?|STOP\s*LOSS(?:ES)?|STOP|STOPS)\s+"
    r"(?:BACK\s+)?(?:TO|AT)\s+"
    r"(?:ENTRY|BE|BREAKEVEN|BREAK\s+EVEN)\b",
    re.IGNORECASE,
)

# TDC occasionally types FREE with extra Es. The explicit price is still unambiguous:
# "+20 / RISK FREEE 4393" means move the stop to 4393.
_RISK_FREE_NUMERIC = re.compile(
    r"\bRISK\s*[- ]?FREE+\s+(?:AT\s+|@\s*)?(\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)

# TDC current-state exit dialect. This deliberately does not match the generic
# provider result "out at BE". It requires the provider to say that THIS setup/trade
# is out, which is an authoritative close instruction when our mapped exposure remains.
_OUT_THIS_SETUP = re.compile(
    r"\b(?:WE\s*(?:['’]RE|ARE)\s+)?OUT\s+(?:OF\s+)?(?:THIS|THE)\s+"
    r"(?:SET\s*UP|SETUP|TRADE|POSITION)\b",
    re.IGNORECASE,
)

# An explicit immediate close must not be erased because a later subordinate clause
# contains "if you wish to hold". A true OR-choice remains protective/optional and is
# left to the existing Day27 policy rather than forcing an exit.
_CLOSE_NOW = re.compile(
    r"\bCLOSE\b.{0,70}\b(?:TRADE|POSITION|SET\s*UP|SETUP|BUY|SELL)\b.{0,30}\bNOW\b"
    r"|\bCLOSE\b.{0,30}\bNOW\b",
    re.IGNORECASE | re.DOTALL,
)
_OR_PROTECTIVE_CHOICE = re.compile(
    r"\bOR\b.{0,100}\b(?:BE|BREAKEVEN|BREAK\s+EVEN|RISK\s*[- ]?FREE)\b",
    re.IGNORECASE | re.DOTALL,
)

# Never upgrade a targeted/partial close into close-all. The native Day27 parser owns
# these forms and already maps them to the correct tranche (for example Close TP1 now
# or Close half now). The override exists only for decisive whole-trade exits that the
# normal optional/result logic could otherwise erase.
_TARGETED_OR_PARTIAL_CLOSE = re.compile(
    r"\bCLOSE\s+(?:THE\s+)?(?:TP\s*\d+|HALF|PARTIAL(?:LY)?|ONE|FIRST|SECOND|THIRD)\b",
    re.IGNORECASE,
)

_installed = False
_original = None
_original_v1 = None


def _price(value: str) -> str | None:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return format(parsed.normalize(), "f")


def _decisive_close(text: str) -> bool:
    if _OUT_THIS_SETUP.search(text):
        return True
    if _TARGETED_OR_PARTIAL_CLOSE.search(text):
        return False
    return _CLOSE_NOW.search(text) is not None and _OR_PROTECTIVE_CHOICE.search(text) is None


def explicit_literal_management(
    raw_text: str,
    *,
    fallback,
) -> Day27ManagementPolicyResult:
    text = raw_text or ""

    # Close is intentionally checked before the fallback. The old optional-language
    # branch could otherwise turn "CLOSE our trade now and set BE if you wish to hold"
    # into BE-only management, leaving the trade open against the provider instruction.
    # Targeted/partial closes are excluded above so their native TP scope is preserved.
    if _decisive_close(text):
        return Day27ManagementPolicyResult(
            ({"type": "close", "target": "all", "value": None},),
            "explicit_literal_close_override",
        )

    existing = fallback(raw_text)
    if existing.actions:
        return existing

    actions: list[dict[str, str | None]] = []
    for pattern in (_NUMERIC_STOP, _RISK_FREE_NUMERIC):
        for match in pattern.finditer(text):
            value = _price(match.group(1))
            if value is not None:
                actions.append(
                    {"type": "edit_stop_loss", "target": "all", "value": value}
                )
    if not actions and _MOVE_TO_ENTRY.search(text):
        actions.append(
            {"type": "move_to_break_even", "target": "all", "value": None}
        )
    if not actions:
        return existing
    return Day27ManagementPolicyResult(
        tuple(actions),
        "explicit_literal_management_override",
    )


def _promote_literal_management(decision: Any, *, raw_text: str, original, **kwargs: Any) -> Any:
    """Run normal V1 policy, then make literal management independent of AI class."""
    result = original(decision, raw_text=raw_text, **kwargs)
    if result.decision == "new_trade" and result.action == "execute":
        return result
    if result.decision == "trade_update" and result.action == "apply_update":
        return result

    assert _original is not None
    policy = explicit_literal_management(raw_text, fallback=_original)
    if not policy.actions:
        return result

    extracted = dict(getattr(result, "extracted", {}) or {})
    normalized = [dict(action) for action in policy.actions]
    extracted["management_actions"] = normalized
    first = normalized[0]
    extracted["update_type"] = first.get("type")
    extracted["update_target"] = first.get("target")
    extracted["update_value"] = first.get("value")
    return replace(
        result,
        decision="trade_update",
        action="apply_update",
        reason=policy.reason,
        extracted=extracted,
    )


def install_literal_management_overrides() -> None:
    """Install once into Day27, V1 policy and the pipeline's imported V1 reference."""
    global _installed, _original, _original_v1
    if _installed:
        return

    import app.ai_message_pipeline as pipeline
    import app.day27_management_policy as day27
    import app.v1_message_policy as v1

    _original = day27.extract_day27_management_actions
    _original_v1 = v1.apply_v1_message_policy

    def wrapped_day27(raw_text: str) -> Day27ManagementPolicyResult:
        assert _original is not None
        return explicit_literal_management(raw_text, fallback=_original)

    def wrapped_v1(decision: Any, *, raw_text: str, **kwargs: Any) -> Any:
        assert _original_v1 is not None
        return _promote_literal_management(
            decision,
            raw_text=raw_text,
            original=_original_v1,
            **kwargs,
        )

    day27.extract_day27_management_actions = wrapped_day27
    v1.extract_day27_management_actions = wrapped_day27
    v1.apply_v1_message_policy = wrapped_v1
    pipeline.apply_v1_message_policy = wrapped_v1
    _installed = True


__all__ = [
    "explicit_literal_management",
    "install_literal_management_overrides",
]
