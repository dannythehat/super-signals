"""Final production semantic pipeline.

Production has one trade interpretation path: deterministic current-message rules for
unambiguous product commands/management, otherwise the source-aware OpenAI supervisor.
The legacy classification/parse tables are never allowed to become an execution
fallback. If semantic interpretation is unavailable, the message is recorded as
non-actionable instead of being re-read by an older restrictive parser.

Recovery/idempotency helpers are owned explicitly here so production never depends on a
removed patch or superseded pipeline generation for durable-decision lookup.

GTMO has one observed provider-specific grammar exception: the standalone whole-message
``Gold buy now`` / ``Gold sell now`` post is a precursor to the structured signal that
follows. That exception is keyed only by the two known GTMO Telegram chat IDs. The exact
same bare NOW wording remains an executable command for every other provider.
"""

from __future__ import annotations

from hashlib import sha256

from sqlalchemy import text

from app.ai_message_pipeline_canonical import (
    CanonicalAiMessagePipeline,
    explicit_management_without_ai,
)
from app.ai_message_supervisor import AiMessageDecision, AiSupervisorError
from app.bare_gold_now_policy import PROFILE, bare_now_side

_GTMO_PRECURSOR_CHAT_IDS = frozenset({-1001640332422, -1002068685216})
_GTMO_PRECURSOR_REASON = "gtmo_precursor_wait_for_structured_signal"


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

    def _is_gtmo_precursor_source(self, source_id) -> bool:
        """Identify the logical GTMO provider by stable Telegram chat identity only."""
        with self._session_factory() as session:
            chat_id = session.execute(
                text("SELECT chat_id FROM sources WHERE id=:source_id LIMIT 1"),
                {"source_id": source_id},
            ).scalar_one_or_none()
        try:
            return int(chat_id) in _GTMO_PRECURSOR_CHAT_IDS
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _gtmo_precursor_decision(raw_text: str, side: str) -> AiMessageDecision:
        """Record GTMO's bare NOW heads-up without creating broker intent."""
        return AiMessageDecision(
            decision="preparation",
            action="ignore",
            confidence=1.0,
            reason=_GTMO_PRECURSOR_REASON,
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
            model="canonical-deterministic-source-policy-v1",
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

        side = bare_now_side(raw_text)
        if side is not None:
            if self._is_gtmo_precursor_source(source_id):
                return self._gtmo_precursor_decision(raw_text, side)
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
