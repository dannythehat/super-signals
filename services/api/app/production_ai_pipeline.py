"""Final production semantic pipeline.

Production has one trade interpretation path: deterministic current-message rules for
unambiguous product commands/management, otherwise the source-aware OpenAI supervisor.
The legacy classification/parse tables are never allowed to become an execution
fallback. If semantic interpretation is unavailable, the message is recorded as
non-actionable instead of being re-read by an older restrictive parser.

Recovery/idempotency helpers are owned explicitly here so production never depends on a
removed patch or superseded pipeline generation for durable-decision lookup.

Some providers use a bare BUY/SELL GOLD post as a heads-up before sending the real
structured signal. For those explicitly known provider chat IDs, a bare precursor is
recorded as preparation only and can never create broker intent. The later detailed
entry/SL/TP signal remains fully executable. Other providers retain their own grammar.
"""

from __future__ import annotations

from hashlib import sha256
import re

from sqlalchemy import text

from app.ai_message_pipeline_canonical import (
    CanonicalAiMessagePipeline,
    explicit_management_without_ai,
)
from app.ai_message_supervisor import AiMessageDecision, AiSupervisorError
from app.bare_gold_now_policy import PROFILE, bare_now_side

# Canonical GTMO, GTMO mirror (kept paused), and FXTradingVision.
_PRECURSOR_CHAT_IDS = frozenset({-1001640332422, -1002068685216, -1001651583302})
_PRECURSOR_REASON = "provider_precursor_wait_for_structured_signal"
_OPEN_GOLD_SIDE = re.compile(r"\b(BUY|BUYS|SELL|SELLS)\b", re.IGNORECASE)


class ProductionAiMessagePipeline(CanonicalAiMessagePipeline):
    """Single production AI/semantic pipeline with no legacy trade-parser fallback."""

    @staticmethod
    def _existing_decision(session, message_id, revision_index: int):
        """Return the exact durable decision for one Telegram revision, if present."""
        return session.execute(
            text(
                """
                SELECT decision, action, decision_source
                FROM ai_message_decisions
                WHERE message_id=:message_id
                  AND revision_index=:revision_index
                LIMIT 1
                """
            ),
            {"message_id": message_id, "revision_index": revision_index},
        ).mappings().first()

    @staticmethod
    def _execution_started(session, signal_id) -> bool:
        """A signal is execution-started once any local broker-intent row exists."""
        return bool(
            session.execute(
                text(
                    """
                    SELECT EXISTS(
                        SELECT 1 FROM positions
                        WHERE signal_id=:signal_id
                    )
                    """
                ),
                {"signal_id": signal_id},
            ).scalar_one()
        )

    def _is_precursor_source(self, source_id) -> bool:
        """Identify provider grammars known to announce before a structured signal."""
        with self._session_factory() as session:
            chat_id = session.execute(
                text("SELECT chat_id FROM sources WHERE id=:source_id LIMIT 1"),
                {"source_id": source_id},
            ).scalar_one_or_none()
        try:
            return int(chat_id) in _PRECURSOR_CHAT_IDS
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _precursor_side(raw_text: str) -> str | None:
        """Return BUY/SELL only for a bare heads-up lacking trade geometry."""
        text_value = " ".join((raw_text or "").strip().split())
        if not text_value:
            return None

        # A real structured signal must never be swallowed by this protection.
        upper = text_value.upper()
        if any(character.isdigit() for character in upper):
            return None
        if re.search(r"\b(?:SL|TP|ENTRY|STOP\s*LOSS|TAKE\s*PROFIT)\b", upper):
            return None

        exact_now = bare_now_side(raw_text)
        if exact_now is not None:
            return exact_now

        # FXTradingVision commonly says e.g. OPEN GOLD BUYS / OPEN GOLD SELLS NOW
        # before posting NEW TRADE IDEA with the entry, stop and targets.
        if not upper.startswith("OPEN") or "GOLD" not in upper:
            return None
        match = _OPEN_GOLD_SIDE.search(upper)
        if match is None:
            return None
        token = match.group(1).upper()
        return "BUY" if token.startswith("BUY") else "SELL"

    @staticmethod
    def _precursor_decision(raw_text: str, side: str) -> AiMessageDecision:
        """Record a provider heads-up without creating signal/broker intent."""
        return AiMessageDecision(
            decision="preparation",
            action="ignore",
            confidence=1.0,
            reason=_PRECURSOR_REASON,
            extracted={
                "symbol": "XAUUSD",
                "side": side,
                "order_type": "market",
                "entry_low": None,
                "entry_high": None,
                "stop_loss": None,
                "take_profits": [],
                "double_lot": False,
                "tp_open": False,
                "update_type": None,
                "update_target": None,
                "update_value": None,
                "provider_claimed_pips": None,
            },
            model="canonical-deterministic-source-policy-v2",
            response_id=None,
            latency_ms=0,
            source="deterministic_source_policy",
            raw_text_sha256=sha256((raw_text or "").encode("utf-8")).hexdigest(),
        )

    def _decide(
        self,
        *,
        source_id,
        telegram_message_id: int,
        revision_index: int,
        raw_text: str,
        source_status: str,
        reply_context: str | None,
        previous_text: str | None,
    ) -> AiMessageDecision:
        management = explicit_management_without_ai(raw_text)
        if management is not None:
            return management

        precursor_side = self._precursor_side(raw_text)
        if precursor_side is not None and self._is_precursor_source(source_id):
            return self._precursor_decision(raw_text, precursor_side)

        side = bare_now_side(raw_text)
        if side is not None:
            return AiMessageDecision(
                decision="new_trade",
                action="execute",
                confidence=1.0,
                reason=PROFILE,
                extracted={
                    "symbol": "XAUUSD",
                    "side": side,
                    "order_type": "market",
                    "entry_low": None,
                    "entry_high": None,
                    "stop_loss": None,
                    "take_profits": [],
                    "double_lot": False,
                    "tp_open": False,
                    "execution_profile": PROFILE,
                    "update_type": None,
                    "update_target": None,
                    "update_value": None,
                    "provider_claimed_pips": None,
                },
                model="canonical-deterministic-v1",
                response_id=None,
                latency_ms=0,
                source="deterministic_no_ai",
                raw_text_sha256=sha256((raw_text or "").encode("utf-8")).hexdigest(),
            )

        profile = self._source_profile(source_id)
        if self._supervisor is None:
            return self._non_actionable_without_ai(
                raw_text,
                reason="semantic_supervisor_unavailable_no_legacy_trade_fallback",
            )

        source_name, recent_source_messages = self._source_context(
            source_id=source_id,
            telegram_message_id=telegram_message_id,
        )
        active_trade_context = self._active_trade_context(source_id=source_id)
        try:
            decide_with_active_context = getattr(
                self._supervisor,
                "decide_with_active_context",
                None,
            )
            if callable(decide_with_active_context):
                semantic = decide_with_active_context(
                    raw_text=raw_text,
                    source_status=source_status,
                    source_name=source_name,
                    active_trade_context=active_trade_context,
                    recent_source_messages=recent_source_messages,
                    reply_context=reply_context,
                    previous_text=previous_text,
                    is_edit=revision_index > 0,
                )
            else:
                semantic = self._supervisor.decide(
                    raw_text=raw_text,
                    source_status=source_status,
                    source_name=source_name,
                    recent_source_messages=recent_source_messages,
                    reply_context=reply_context,
                    previous_text=previous_text,
                    is_edit=revision_index > 0,
                )
        except AiSupervisorError:
            return self._non_actionable_without_ai(
                raw_text,
                reason="semantic_supervisor_unavailable_no_legacy_trade_fallback",
            )

        semantic = self._apply_profile(semantic, profile)
        return self._literal_order_type_precedence(semantic, raw_text)


__all__ = ["ProductionAiMessagePipeline"]
