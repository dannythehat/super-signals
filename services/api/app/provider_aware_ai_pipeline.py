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

_CONTEXT_LIMIT = 16
_CONTEXT_TEXT_LIMIT = 900


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

    @staticmethod
    def _playbook_context(profile_snapshot: dict[str, Any]) -> dict[str, Any]:
        """Expose safe provider habits without historical prices or outcomes."""
        metadata = profile_snapshot.get("profile_metadata")
        if not isinstance(metadata, dict):
            return {}
        playbook = metadata.get("provider_playbook_v1")
        if not isinstance(playbook, dict):
            return {}
        allowed = {
            "version",
            "knowledge_state",
            "trading_style",
            "cadence_bucket",
            "management_bucket",
            "signals_observed",
            "preferred_order_type",
            "order_type_counts",
            "usual_tp_count",
            "tp_count_distribution",
            "explicit_stop_loss_pct",
            "open_runner_pct",
            "risk_multiplier_above_one_pct",
            "entry_zone_pct",
            "reply_linked_management_pct",
            "standalone_management_pct",
            "edited_message_pct",
            "management_phrase_families",
            "management_language_coverage_pct",
            "unmapped_management_count",
            "covered_management_examples_masked",
            "unmapped_management_examples_masked",
            "safety_note",
        }
        return {
            key: value
            for key, value in playbook.items()
            if key in allowed
        }

    def _recent_context_as_of(
        self,
        *,
        source_id: UUID,
        telegram_message_id: int,
        as_of: datetime,
    ) -> list[dict[str, Any]]:
        """Return prior same-source messages exactly as they were knowable at ``as_of``.

        A later edit or deletion must never leak backwards into interpretation of an
        earlier message. For each older post, choose only the latest revision whose
        ``edited_at`` was already observed by the current message timestamp.
        """
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT m.telegram_message_id,m.posted_at,m.raw_payload,
                           COALESCE(r.raw_text,m.raw_text) AS effective_text,
                           COALESCE(r.revision_index,0) AS effective_revision_index
                    FROM messages m
                    LEFT JOIN LATERAL (
                        SELECT mr.raw_text,mr.revision_index
                        FROM message_revisions mr
                        WHERE mr.message_id=m.id
                          AND mr.edited_at<=:as_of
                        ORDER BY mr.revision_index DESC,mr.edited_at DESC
                        LIMIT 1
                    ) r ON TRUE
                    WHERE m.source_id=:source_id
                      AND m.telegram_message_id<:telegram_message_id
                      AND m.posted_at<=:as_of
                      AND (m.deleted_at IS NULL OR m.deleted_at>:as_of)
                    ORDER BY m.telegram_message_id DESC
                    LIMIT :limit
                    """
                ),
                {
                    "source_id": source_id,
                    "telegram_message_id": telegram_message_id,
                    "as_of": as_of,
                    "limit": _CONTEXT_LIMIT,
                },
            ).mappings().all()

        context: list[dict[str, Any]] = []
        for row in reversed(rows):
            payload = row["raw_payload"] if isinstance(row["raw_payload"], dict) else {}
            reply_to = payload.get("reply_to_message_id") if payload else None
            posted = row["posted_at"]
            revision_index = int(row["effective_revision_index"] or 0)
            context.append(
                {
                    "telegram_message_id": int(row["telegram_message_id"]),
                    "posted_at": posted.isoformat() if isinstance(posted, datetime) else str(posted or ""),
                    "reply_to_message_id": int(reply_to) if isinstance(reply_to, int) else reply_to,
                    "revision_index_as_of_message": revision_index,
                    "was_edited_as_of_message": revision_index > 0,
                    "text": str(row["effective_text"] or "")[:_CONTEXT_TEXT_LIMIT],
                }
            )
        return context

    def _source_context(
        self,
        *,
        source_id: UUID,
        telegram_message_id: int,
    ) -> tuple[str | None, list[dict[str, Any]]]:
        with self._session_factory() as session:
            current = session.execute(
                text(
                    """
                    SELECT m.posted_at,COALESCE(NULLIF(s.chat_title,''),NULLIF(s.source_alias,'')) AS source_name
                    FROM messages m
                    JOIN sources s ON s.id=m.source_id
                    WHERE m.source_id=:source_id
                      AND m.telegram_message_id=:telegram_message_id
                    ORDER BY m.posted_at DESC,m.id DESC
                    LIMIT 1
                    """
                ),
                {"source_id": source_id, "telegram_message_id": telegram_message_id},
            ).mappings().first()

            if current is None or not isinstance(current["posted_at"], datetime):
                return super()._source_context(
                    source_id=source_id,
                    telegram_message_id=telegram_message_id,
                )

            posted_at = self._utc(current["posted_at"])
            source_name = str(current["source_name"]) if current["source_name"] else None
            profile = resolve_provider_profile_as_of(
                session,
                source_id=source_id,
                as_of=posted_at,
            )

        context = self._recent_context_as_of(
            source_id=source_id,
            telegram_message_id=telegram_message_id,
            as_of=posted_at,
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
            "provider_playbook": self._playbook_context(profile.profile_snapshot),
            "communication_traits": profile.communication_traits,
            "safety_note": (
                "This immutable profile and prior-message revisions were already knowable "
                "at the current message timestamp. Provider Footprint supplies behavioural "
                "descriptors only. Historical numeric values are not execution evidence; "
                "current-message/direct-reply evidence remains mandatory for execution."
            ),
        }
        return source_name, [profile_context, *context]


__all__ = ["ProviderAwareProductionAiPipeline"]