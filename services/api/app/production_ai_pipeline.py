"""Final production semantic pipeline.

The exact standalone Gold/XAUUSD NOW command is deterministic product policy and never
requires OpenAI to recognise it. Every other message delegates to the canonical
source-aware semantic pipeline.
"""

from __future__ import annotations

from hashlib import sha256

from app.ai_message_pipeline_canonical import CanonicalAiMessagePipeline
from app.ai_message_supervisor import AiMessageDecision
from app.bare_gold_now_policy import PROFILE, bare_now_side


class ProductionAiMessagePipeline(CanonicalAiMessagePipeline):
    """Single production AI/semantic pipeline."""

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
        side = bare_now_side(raw_text)
        if side is not None and revision_index == 0:
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
        return super()._decide(
            source_id=source_id,
            telegram_message_id=telegram_message_id,
            revision_index=revision_index,
            raw_text=raw_text,
            source_status=source_status,
            reply_context=reply_context,
            previous_text=previous_text,
        )


__all__ = ["ProductionAiMessagePipeline"]
