"""Production correction for AI order-type guesses on plain provider ranges.

A provider message such as ``BUY GOLD @ 4401/4396`` is a literal market entry zone
unless the provider explicitly writes LIMIT, STOP or PENDING. OpenAI may classify a
price range as ``order_type=pending``; that semantic guess must never override the
provider's literal broker instruction and cause an otherwise complete signal to be
rejected.

This wrapper changes only the narrow ``pending_order_type_ambiguous`` failure where
there is no literal pending-order wording. Explicit pending wording remains fail-closed
unless the deterministic critical-entry parser can prove the broker order type.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

import app.v1_message_policy as v1_message_policy

_LITERAL_PENDING = re.compile(
    r"\b(?:BUY|SELL)\s+(?:LIMITS?|STOPS?)\b|\bPENDING\b",
    re.IGNORECASE,
)


def install_plain_range_order_type_override() -> None:
    """Install the literal-evidence precedence rule once per process."""
    current = v1_message_policy.apply_v1_message_policy
    if getattr(current, "_plain_range_order_type_override_installed", False):
        return

    original = current

    def wrapped(
        decision: Any,
        *,
        raw_text: str,
        is_edit: bool = False,
        original_has_signal: bool | None = None,
        previous_text: str | None = None,
    ):
        result = original(
            decision,
            raw_text=raw_text,
            is_edit=is_edit,
            original_has_signal=original_has_signal,
            previous_text=previous_text,
        )

        # Only correct an AI-invented pending classification. Literal LIMIT/STOP/
        # PENDING wording retains the existing deterministic fail-closed path.
        if (
            getattr(decision, "decision", None) == "new_trade"
            and getattr(result, "action", None) == "skip"
            and getattr(result, "reason", None) == "pending_order_type_ambiguous"
            and _LITERAL_PENDING.search(raw_text or "") is None
        ):
            extracted = dict(getattr(decision, "extracted", {}) or {})
            if str(extracted.get("order_type") or "").strip().lower() == "pending":
                extracted["order_type"] = "market"
                corrected = replace(decision, extracted=extracted)
                return original(
                    corrected,
                    raw_text=raw_text,
                    is_edit=is_edit,
                    original_has_signal=original_has_signal,
                    previous_text=previous_text,
                )

        return result

    wrapped._plain_range_order_type_override_installed = True  # type: ignore[attr-defined]
    v1_message_policy.apply_v1_message_policy = wrapped


__all__ = ["install_plain_range_order_type_override"]
