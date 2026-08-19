"""Canonical provider-message decision pipeline.

Deterministic evidence is used first where it is mechanically conclusive; OpenAI is the
semantic resolver for provider language that is not. Source profiles are exact database
identities and may supply only known instrument identity for dedicated Gold sources.
They never supply an entry, SL, TP, order type, size or management target.
"""

from __future__ import annotations

import re
from dataclasses import replace
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.ai_message_supervisor import AiMessageDecision
from app.ai_source_aware_pipeline import SourceAwareAiMessagePipeline

_LITERAL_PENDING = re.compile(
    r"\b(?:BUY|SELL)\s+(?:LIMITS?|STOPS?)(?:\s+ORDER)?\b|\bPENDING\b",
    re.IGNORECASE,
)
_PRESENT_TENSE_NUMERIC_ENTRY = re.compile(
    r"\b(?:I\s*['’]?\s*M|I\s+AM)\s+"
    r"(?:BUYING|SELLING|SELING|SELLIMG)\s+"
    r"(?:(?:NOW|IF\s+WE\s+TAP(?:\s+IT)?)\s+)?"
    r"(?:(?:GOLD|XAUUSD)\s+)?"
    r"\d+(?:\.\d+)?\b",
    re.IGNORECASE | re.MULTILINE,
)

_SOURCE_PROFILES = {
    "the gold club - tgc": "tgc_xauusd",
    "tdc v2 💎 (new)": "tdc_xauusd",
}


def _empty_extracted() -> dict[str, Any]:
    return {
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
    }


def explicit_management_without_ai(raw_text: str) -> AiMessageDecision | None:
    """Return deterministic semantic evidence for explicit broker management."""
    from app.day27_management_policy import extract_day27_management_actions

    policy = extract_day27_management_actions(raw_text or "")
    if not policy.actions:
        return None
    actions = [dict(action) for action in policy.actions]
    first = actions[0]
    extracted = _empty_extracted()
    extracted.update(
        {
            "management_actions": actions,
            "update_type": first.get("type"),
            "update_target": first.get("target"),
            "update_value": first.get("value"),
        }
    )
    return AiMessageDecision(
        decision="trade_update",
        action="apply_update",
        confidence=1.0,
        reason=f"deterministic_no_ai_{policy.reason}",
        extracted=extracted,
        model="canonical-deterministic-v1",
        response_id=None,
        latency_ms=0,
        source="deterministic_no_ai",
        raw_text_sha256=sha256((raw_text or "").encode("utf-8")).hexdigest(),
    )


class CanonicalAiMessagePipeline(SourceAwareAiMessagePipeline):
    """One production decision policy for every provider message/revision."""

    def _source_profile(self, source_id: UUID) -> str | None:
        with self._session_factory() as session:
            source_name = session.execute(
                text(
                    """
                    SELECT COALESCE(NULLIF(chat_title,''),NULLIF(source_alias,''))
                    FROM sources WHERE id=:source_id LIMIT 1
                    """
                ),
                {"source_id": source_id},
            ).scalar_one_or_none()
        return _SOURCE_PROFILES.get(str(source_name or "").strip().lower())

    @staticmethod
    def _apply_profile(decision: AiMessageDecision, profile: str | None) -> AiMessageDecision:
        if profile is None or decision.decision != "new_trade":
            return decision
        extracted = dict(decision.extracted or {})
        extracted["source_profile"] = profile
        if not extracted.get("symbol"):
            extracted["symbol"] = "XAUUSD"
        return replace(decision, extracted=extracted)

    @staticmethod
    def _literal_order_type_precedence(
        decision: AiMessageDecision,
        raw_text: str,
    ) -> AiMessageDecision:
        if decision.decision != "new_trade":
            return decision
        extracted = dict(decision.extracted or {})
        if (
            str(extracted.get("order_type") or "").strip().lower() == "pending"
            and _LITERAL_PENDING.search(raw_text or "") is None
        ):
            extracted["order_type"] = "market"
            return replace(decision, extracted=extracted)
        return decision

    def _decide(
        self,
        *,
        source_id: UUID,
        telegram_message_id: int,
        revision_index: int,
        raw_text: str,
        source_status: str,
        reply_context: str | None,
        previous_text: str | None,
    ) -> AiMessageDecision:
        # Explicit close/SL/BE/partial/cancel instructions cannot be lost to an AI
        # outage or weak chatter classification. Mechanical targeting remains in the
        # management policy/router and is still fail-closed.
        management = explicit_management_without_ai(raw_text)
        if management is not None:
            return management

        # A parse that has already passed deterministic validation needs no AI call.
        deterministic = self._deterministic_fallback(
            source_id=source_id,
            telegram_message_id=telegram_message_id,
            revision_index=revision_index,
            raw_text=raw_text,
        )
        profile = self._source_profile(source_id)
        if deterministic.decision == "new_trade" and deterministic.action == "execute":
            return self._literal_order_type_precedence(
                self._apply_profile(
                    replace(
                        deterministic,
                        model="canonical-deterministic-v1",
                        source="deterministic_no_ai",
                        reason="deterministic_known_trade_no_ai",
                    ),
                    profile,
                ),
                raw_text,
            )

        # Known non-trade chatter can skip OpenAI, except provider text that is itself
        # a present-tense numeric entry. Those informal dialects must reach semantics.
        if (
            deterministic.decision == "chatter"
            and deterministic.action == "ignore"
            and _PRESENT_TENSE_NUMERIC_ENTRY.search(raw_text or "") is None
        ):
            return deterministic

        semantic = super()._decide(
            source_id=source_id,
            telegram_message_id=telegram_message_id,
            revision_index=revision_index,
            raw_text=raw_text,
            source_status=source_status,
            reply_context=reply_context,
            previous_text=previous_text,
        )
        semantic = self._apply_profile(semantic, profile)
        return self._literal_order_type_precedence(semantic, raw_text)


__all__ = ["CanonicalAiMessagePipeline", "explicit_management_without_ai"]
