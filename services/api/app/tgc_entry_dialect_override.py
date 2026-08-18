"""Keep observed TGC present-tense numeric entries out of deterministic chatter.

TGC frequently writes informal entry instructions such as ``Im selling 4390`` and
occasionally misspells them (``Im seling 4390``) or splits the verb onto a new line.
These forms are trade-like and must reach semantic interpretation. This override does
not invent SL/TP and therefore does not make incomplete TGC entries executable by
itself.
"""

from __future__ import annotations

import re

_TGC_NUMERIC_ENTRY = re.compile(
    r"\b(?:I\s*['’]?\s*M|I\s+AM)\s+"
    r"(BUYING|SELLING|SELING)\s+"
    r"(?:(?:NOW|IF\s+WE\s+TAP(?:\s+IT)?)\s+)?"
    r"(?:(?:GOLD|XAUUSD)\s+)?"
    r"(\d+(?:\.\d+)?)\b",
    re.IGNORECASE | re.MULTILINE,
)

_installed = False


def install_tgc_entry_dialect_override() -> None:
    global _installed
    if _installed:
        return

    import app.message_classifier as classifier

    original = classifier.classify_message
    if getattr(original, "_tgc_entry_dialect", False):
        _installed = True
        return

    def wrapped(raw_text: str, *, reply_to_message_id: int | None = None):
        result = original(raw_text, reply_to_message_id=reply_to_message_id)
        if result.classification == "chatter" and _TGC_NUMERIC_ENTRY.search(raw_text or ""):
            return classifier.ClassificationResult(
                classification="uncertain",
                decision_status="review",
                reason="Present-tense numeric provider entry must reach semantic interpretation, including observed TGC spelling/newline variants.",
                matched_rules=("tgc_present_tense_numeric_entry",),
            )
        return result

    wrapped._tgc_entry_dialect = True  # type: ignore[attr-defined]
    classifier.classify_message = wrapped
    _installed = True


__all__ = ["install_tgc_entry_dialect_override"]
