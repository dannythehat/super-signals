"""Source-level profile for The Gold Club (TGC) shorthand Gold entries.

TGC is a dedicated Gold/XAUUSD source but frequently omits the instrument token from
individual entry posts (for example ``Im buying 4395``). The semantic supervisor already
understands these as XAUUSD. This profile lets the mechanical V1 gate trust the *source
identity* for the instrument only; it never donates an entry, SL, TP, order type, or any
other trade number.

Observed TGC spelling mistakes such as ``seling`` and ``sellimg`` are normalized only in
the temporary mechanical-policy text. The provider's raw Telegram text remains unchanged
in evidence/canonical storage.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from sqlalchemy import text

_TGC_TITLE = "the gold club - tgc"
_PROFILE = "tgc_xauusd"
_INSTRUMENT = re.compile(r"\b(?:XAUUSD|GOLD)\b", re.IGNORECASE)
_SELL_TYPOS = re.compile(r"\b(?:SELING|SELLIMG)\b", re.IGNORECASE)
_source_cache: dict[str, bool] = {}
_installed = False


def _is_tgc_source(pipeline: Any, source_id: Any) -> bool:
    key = str(source_id)
    cached = _source_cache.get(key)
    if cached is not None:
        return cached
    with pipeline._session_factory() as session:
        source_name = session.execute(
            text(
                """
                SELECT COALESCE(NULLIF(chat_title, ''), NULLIF(source_alias, ''))
                FROM sources
                WHERE id=:source_id
                LIMIT 1
                """
            ),
            {"source_id": source_id},
        ).scalar_one_or_none()
    matched = str(source_name or "").strip().lower() == _TGC_TITLE
    _source_cache[key] = matched
    return matched


def _wrap_decider(cls: type[Any]) -> None:
    original = cls._decide
    if getattr(original, "_tgc_source_profile", False):
        return

    def wrapped(self: Any, **kwargs: Any):
        decision = original(self, **kwargs)
        # The TGC source profile only changes NEW ENTRY interpretation. Explicit
        # management must remain database-free and immediate, so never perform a
        # provider lookup for close/SL/BE/partial/cancel decisions.
        if decision.decision != "new_trade":
            return decision
        if not _is_tgc_source(self, kwargs.get("source_id")):
            return decision
        extracted = dict(decision.extracted)
        extracted["source_profile"] = _PROFILE
        if not extracted.get("symbol"):
            extracted["symbol"] = "XAUUSD"
        return replace(decision, extracted=extracted)

    wrapped._tgc_source_profile = True  # type: ignore[attr-defined]
    cls._decide = wrapped


def _profile_policy_text(raw_text: str) -> str:
    value = _SELL_TYPOS.sub("selling", raw_text or "")
    if _INSTRUMENT.search(value) is None:
        value = f"GOLD\n{value}"
    return value


def _install_policy_wrapper() -> None:
    import app.ai_message_pipeline as pipeline_module
    import app.v1_message_policy as policy

    original = policy.apply_v1_message_policy
    if getattr(original, "_tgc_source_profile", False):
        pipeline_module.apply_v1_message_policy = original
        return

    def wrapped(decision: Any, *, raw_text: str, **kwargs: Any):
        profile = str((decision.extracted or {}).get("source_profile") or "")
        policy_text = _profile_policy_text(raw_text) if profile == _PROFILE else raw_text
        result = original(decision, raw_text=policy_text, **kwargs)
        if profile == _PROFILE:
            extracted = dict(result.extracted)
            extracted["source_profile"] = _PROFILE
            if result.decision == "new_trade":
                extracted["symbol"] = "XAUUSD"
            result = replace(result, extracted=extracted)
        return result

    wrapped._tgc_source_profile = True  # type: ignore[attr-defined]
    policy.apply_v1_message_policy = wrapped
    # ai_message_pipeline imported the function directly, so update that binding too.
    pipeline_module.apply_v1_message_policy = wrapped


def install_tgc_source_profile_override() -> None:
    global _installed
    if _installed:
        return
    from app.ai_message_pipeline import AiMessagePipeline
    from app.ai_source_aware_pipeline import SourceAwareAiMessagePipeline

    _wrap_decider(AiMessagePipeline)
    _wrap_decider(SourceAwareAiMessagePipeline)
    _install_policy_wrapper()
    _installed = True


__all__ = ["install_tgc_source_profile_override"]
