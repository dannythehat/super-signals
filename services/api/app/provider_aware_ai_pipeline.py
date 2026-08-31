"""Production AI pipeline enriched with non-numeric Provider Lab communication context."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.production_ai_pipeline import ProductionAiMessagePipeline


class ProviderAwareProductionAiPipeline(ProductionAiMessagePipeline):
    """Add learned provider style/traits without donating execution numbers to the model."""

    def _source_context(
        self,
        *,
        source_id: UUID,
        telegram_message_id: int,
    ) -> tuple[str | None, list[dict[str, Any]]]:
        source_name, context = super()._source_context(
            source_id=source_id,
            telegram_message_id=telegram_message_id,
        )
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT style,interpretation_readiness,profile_metadata
                    FROM provider_research_profiles WHERE source_id=:source_id LIMIT 1
                    """
                ),
                {"source_id": source_id},
            ).mappings().first()
        if row is None:
            return source_name, context

        metadata = row["profile_metadata"] if isinstance(row["profile_metadata"], dict) else {}
        profile_context = {
            "context_type": "provider_research_profile",
            "style": str(row["style"] or "unknown"),
            "interpretation_readiness": float(row["interpretation_readiness"] or 0),
            # Only behavioural labels/counts are supplied. Never pass historical prices,
            # inferred SL/TP values or performance outcomes into semantic execution input.
            "communication_traits": {
                key: metadata.get(key)
                for key in (
                    "sample_signals_per_day",
                    "pending_signal_messages",
                    "scalp_language",
                    "swing_language",
                    "management_intensity",
                    "uses_partial_language",
                    "uses_runner_language",
                    "uses_breakeven_language",
                    "uses_layer_language",
                    "uses_reentry_language",
                )
                if key in metadata
            },
        }
        return source_name, [profile_context, *context]


__all__ = ["ProviderAwareProductionAiPipeline"]
