"""Production AI pipeline enriched with point-in-time Provider Lab context."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.production_ai_pipeline import ProductionAiMessagePipeline
from app.provider_adaptive_profile import AdaptiveProviderProfileService
from app.provider_footprint_v1 import ProviderFootprintService
from app.provider_profile_pit import resolve_provider_profile_as_of


class ProviderAwareProductionAiPipeline(ProductionAiMessagePipeline):
    """Add only provider grammar/behaviour that was already knowable for this message."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._adaptive_profiles = AdaptiveProviderProfileService(self._session_factory)
        self._provider_footprints = ProviderFootprintService(self._session_factory)

    def refresh_all_provider_profiles(self) -> int:
        """Refresh adaptive grammar plus the semantic-stable Provider Footprint."""
        with self._session_factory() as session:
            source_ids = session.execute(
                text(
                    """
                    SELECT id FROM sources
                    WHERE status IN ('testing','shadow','live')
                    ORDER BY created_at,id
                    """
                )
            ).scalars().all()
        refreshed = 0
        for value in source_ids:
            source_id = UUID(str(value))
            self._adaptive_profiles.invalidate(source_id)
            self._adaptive_profiles.get(source_id)
            self._provider_footprints.refresh(source_id)
            refreshed += 1
        return refreshed

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def _footprint_context(profile_snapshot: dict[str, Any]) -> dict[str, Any]:
        """Expose only the sanitised behavioural subset, never historical geometry."""
        metadata = profile_snapshot.get("profile_metadata")
        if not isinstance(metadata, dict):
            return {}
        footprint = metadata.get("footprint_v1")
        if not isinstance(footprint, dict):
            return {}
        context = footprint.get("interpretation_context")
        return dict(context) if isinstance(context, dict) else {}

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
            posted_at = session.execute(
                text(
                    """
                    SELECT posted_at
                    FROM messages
                    WHERE source_id=:source_id
                      AND telegram_message_id=:telegram_message_id
                      AND deleted_at IS NULL
                    ORDER BY posted_at DESC,id DESC
                    LIMIT 1
                    """
                ),
                {
                    "source_id": source_id,
                    "telegram_message_id": telegram_message_id,
                },
            ).scalar_one_or_none()
            profile = (
                resolve_provider_profile_as_of(
                    session,
                    source_id=source_id,
                    as_of=self._utc(posted_at),
                )
                if isinstance(posted_at, datetime)
                else None
            )

        # Fail closed for legacy/backfilled messages. Never rebuild current provider
        # learning here: doing so would leak future behaviour into an older message.
        if profile is None:
            return source_name, context

        profile_context = {
            "context_type": "provider_research_profile_pit",
            "provider_profile_version": profile.version_no,
            "provider_profile_effective_at": profile.effective_at.isoformat(),
            "provider_profile_fingerprint": profile.snapshot_fingerprint,
            "style": profile.style,
            "interpretation_readiness": profile.interpretation_readiness,
            "adaptive_language_profile": profile.adaptive_language_profile,
            "provider_footprint": self._footprint_context(profile.profile_snapshot),
            "communication_traits": profile.communication_traits,
            "safety_note": (
                "This immutable profile was already effective at the message timestamp. "
                "Provider Footprint supplies behavioural descriptors only. Historical "
                "numeric values are not execution evidence; current-message/direct-reply "
                "evidence remains mandatory for execution."
            ),
        }
        return source_name, [profile_context, *context]


__all__ = ["ProviderAwareProductionAiPipeline"]