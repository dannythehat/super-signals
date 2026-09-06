"""Point-in-time provider profile resolution for forward-only Provider Intelligence.

Day 8 makes the immutable Day 7 provider profile-version ledger the only provider
learning source for historical/replay context.  This module deliberately has no
fallback to the mutable ``provider_research_profiles`` cache.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

PIT_RESOLVED = "resolved"
PIT_LEGACY_UNRESOLVABLE = "legacy_unresolvable"


@dataclass(frozen=True, slots=True)
class ProviderProfilePIT:
    id: UUID
    source_id: UUID
    version_no: int
    effective_at: datetime
    snapshot_fingerprint: str
    source_identity: dict[str, Any]
    profile_snapshot: dict[str, Any]

    @property
    def style(self) -> str:
        return str(self.profile_snapshot.get("style") or "unknown")

    @property
    def interpretation_readiness(self) -> float:
        raw = self.profile_snapshot.get("interpretation_readiness")
        try:
            return float(raw or 0)
        except (TypeError, ValueError):
            return 0.0

    @property
    def adaptive_language_profile(self) -> dict[str, Any]:
        metadata = self.profile_snapshot.get("profile_metadata")
        if not isinstance(metadata, dict):
            return {}
        adaptive = metadata.get("adaptive_v1")
        if not isinstance(adaptive, dict):
            return {}
        language = adaptive.get("language")
        return dict(language) if isinstance(language, dict) else {}

    @property
    def communication_traits(self) -> dict[str, Any]:
        metadata = self.profile_snapshot.get("profile_metadata")
        if not isinstance(metadata, dict):
            return {}
        keys = (
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
        return {key: metadata.get(key) for key in keys if key in metadata}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def resolve_provider_profile_as_of(
    session: Session,
    *,
    source_id: UUID,
    as_of: datetime,
) -> ProviderProfilePIT | None:
    """Return only provider knowledge already effective at ``as_of``.

    There is intentionally no fallback to current mutable profile state.  A
    timestamp earlier than the Day 7 bootstrap origin therefore returns None and
    must be treated as legacy/unresolvable by callers.
    """

    row = session.execute(
        text(
            """
            SELECT id,source_id,version_no,effective_at,snapshot_fingerprint,
                   source_identity,profile_snapshot
            FROM provider_research_profile_versions
            WHERE source_id=:source_id AND effective_at <= :as_of
            ORDER BY effective_at DESC,version_no DESC
            LIMIT 1
            """
        ),
        {"source_id": source_id, "as_of": _utc(as_of)},
    ).mappings().first()
    if row is None:
        return None
    effective_at = row["effective_at"]
    if not isinstance(effective_at, datetime):
        raise ValueError("provider_profile_effective_at_invalid")
    return ProviderProfilePIT(
        id=UUID(str(row["id"])),
        source_id=UUID(str(row["source_id"])),
        version_no=int(row["version_no"]),
        effective_at=_utc(effective_at),
        snapshot_fingerprint=str(row["snapshot_fingerprint"]),
        source_identity=_dict(row["source_identity"]),
        profile_snapshot=_dict(row["profile_snapshot"]),
    )


__all__ = [
    "PIT_LEGACY_UNRESOLVABLE",
    "PIT_RESOLVED",
    "ProviderProfilePIT",
    "resolve_provider_profile_as_of",
]
