"""Production AI pipeline enriched with adaptive Provider Lab communication context."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.production_ai_pipeline import ProductionAiMessagePipeline
from app.provider_adaptive_profile import AdaptiveProviderProfileService


class ProviderAwareProductionAiPipeline(ProductionAiMessagePipeline):
    """Add learned provider grammar without donating historical execution numbers."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._adaptive_profiles = AdaptiveProviderProfileService(self._session_factory)

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

        adaptive = self._adaptive_profiles.get(source_id).language_context
        metadata = (
            row["profile_metadata"]
            if row is not None and isinstance(row["profile_metadata"], dict)
            else {}
        )
        profile_context = {
            "context_type": "provider_research_profile",
            "style": (
                str(row["style"] or "unknown")
                if row is not None
                else adaptive.get("cadence_bucket", "unknown")
            ),
            "interpretation_readiness": (
                float(row["interpretation_readiness"] or 0) if row is not None else 0.0
            ),
            "adaptive_language_profile": adaptive,
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
            "safety_note": (
                "This profile is semantic context only. Masked examples contain no usable historical "
                "prices. Current-message/direct-reply evidence remains mandatory for execution."
            ),
        }
        return source_name, [profile_context, *context]


__all__ = ["ProviderAwareProductionAiPipeline"]
