"""Immutable per-signal Provider Intelligence context attachment.

Day 10 persists only research provenance. It never calls broker/member execution paths and
never mutates the shadow trade after enrollment. Transient AIDY lookup failures remain
retryable; a proven historical `pit_context_stale` miss is persisted once and excluded
from future polls because it cannot become PIT-valid later without rewriting history.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.aidy_context_client import AidyContextClient, AidyContextTerminalMiss
from app.provider_aidy_context_join import ProviderContextJoinBlocked, join_provider_to_aidy_context

logger = logging.getLogger(__name__)

CONTRACT_VERSION = "provider_aidy_context_attachment_v1"
TERMINAL_MISS_CONTRACT_VERSION = "provider_aidy_context_terminal_miss_v1"
_DEFAULT_BATCH_LIMIT = 50


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("provider_context_timestamp_timezone_required")
    return value.astimezone(UTC)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ContextAttachmentCandidate:
    signal_id: UUID
    source_id: UUID
    message_id: UUID
    signal_posted_at: datetime
    provider_profile_version_id: UUID
    provider_profile_version_no: int
    provider_profile_effective_at: datetime


class ProviderContextAttachmentResolver:
    """Attach one immutable AIDY context packet to each PIT-clean research signal."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        client: AidyContextClient,
        *,
        batch_limit: int = _DEFAULT_BATCH_LIMIT,
    ) -> None:
        if batch_limit <= 0 or batch_limit > 200:
            raise ValueError("provider_context_batch_limit_invalid")
        self._session_factory = session_factory
        self._client = client
        self._batch_limit = batch_limit
        self._last_terminal_misses = 0

    @property
    def last_terminal_misses(self) -> int:
        return self._last_terminal_misses

    def _candidates(self) -> list[ContextAttachmentCandidate]:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT DISTINCT ON (t.signal_id)
                           t.signal_id,t.source_id,t.message_id,t.signal_posted_at,
                           t.provider_profile_version_id,t.provider_profile_version_no,
                           t.provider_profile_effective_at
                    FROM shadow_trades t
                    WHERE t.provider_profile_pit_status='resolved'
                      AND t.provider_profile_version_id IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1
                          FROM provider_signal_context_attachments a
                          WHERE a.signal_id=t.signal_id
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM provider_signal_context_terminal_misses m
                          WHERE m.signal_id=t.signal_id
                      )
                    ORDER BY t.signal_id,t.entry_index
                    LIMIT :limit
                    """
                ),
                {"limit": self._batch_limit},
            ).mappings().all()
        candidates: list[ContextAttachmentCandidate] = []
        for row in rows:
            posted_at = row["signal_posted_at"]
            profile_at = row["provider_profile_effective_at"]
            if not isinstance(posted_at, datetime) or not isinstance(profile_at, datetime):
                continue
            candidates.append(
                ContextAttachmentCandidate(
                    signal_id=UUID(str(row["signal_id"])),
                    source_id=UUID(str(row["source_id"])),
                    message_id=UUID(str(row["message_id"])),
                    signal_posted_at=_utc(posted_at),
                    provider_profile_version_id=UUID(str(row["provider_profile_version_id"])),
                    provider_profile_version_no=int(row["provider_profile_version_no"]),
                    provider_profile_effective_at=_utc(profile_at),
                )
            )
        return candidates

    @staticmethod
    def _payload(candidate: ContextAttachmentCandidate, joined: Any) -> dict[str, Any]:
        aidy = joined.aidy
        return {
            "contract_version": CONTRACT_VERSION,
            "signal_id": str(candidate.signal_id),
            "source_id": str(candidate.source_id),
            "message_id": str(candidate.message_id),
            "signal_posted_at": candidate.signal_posted_at.isoformat(),
            "provider_profile": {
                "version_id": str(candidate.provider_profile_version_id),
                "version_no": candidate.provider_profile_version_no,
                "effective_at": candidate.provider_profile_effective_at.isoformat(),
            },
            "aidy": {
                "requested_as_of_utc": aidy.requested_as_of_utc.isoformat(),
                "context_as_of_utc": aidy.context_as_of_utc.isoformat(),
                "context_lag_seconds": aidy.context_lag_seconds,
                "context_hash": aidy.context_hash,
                "snapshot": {
                    "id": aidy.snapshot_id,
                    "digest": aidy.snapshot_digest,
                    "archive_key": aidy.snapshot_archive_key,
                },
                "session": aidy.session,
                "regime": aidy.regime,
                "data_quality": aidy.data_quality,
                "market": aidy.market,
                "provenance": aidy.provenance,
            },
            "point_in_time_clean": True,
            "research_only": True,
            "live_money_execution_allowed": False,
        }

    def _persist(self, candidate: ContextAttachmentCandidate, joined: Any) -> bool:
        payload = self._payload(candidate, joined)
        digest = _digest(payload)
        aidy = joined.aidy
        with self._session_factory() as session:
            inserted = session.execute(
                text(
                    """
                    INSERT INTO provider_signal_context_attachments(
                        id,signal_id,source_id,message_id,signal_posted_at,
                        provider_profile_version_id,provider_profile_version_no,
                        provider_profile_effective_at,aidy_requested_as_of_utc,
                        aidy_context_as_of_utc,aidy_context_lag_seconds,aidy_context_hash,
                        aidy_snapshot_id,aidy_snapshot_digest,aidy_snapshot_archive_key,
                        session_json,regime_json,data_quality_json,market_json,provenance_json,
                        attachment_payload,attachment_digest,contract_version
                    ) VALUES (
                        :id,:signal_id,:source_id,:message_id,:signal_posted_at,
                        :profile_id,:profile_no,:profile_at,:requested_at,:context_at,:lag,:context_hash,
                        :snapshot_id,:snapshot_digest,:snapshot_archive_key,
                        CAST(:session_json AS jsonb),CAST(:regime_json AS jsonb),
                        CAST(:data_quality_json AS jsonb),CAST(:market_json AS jsonb),
                        CAST(:provenance_json AS jsonb),CAST(:payload AS jsonb),:digest,:contract_version
                    )
                    ON CONFLICT (signal_id) DO NOTHING
                    RETURNING id
                    """
                ),
                {
                    "id": uuid4(),
                    "signal_id": candidate.signal_id,
                    "source_id": candidate.source_id,
                    "message_id": candidate.message_id,
                    "signal_posted_at": candidate.signal_posted_at,
                    "profile_id": candidate.provider_profile_version_id,
                    "profile_no": candidate.provider_profile_version_no,
                    "profile_at": candidate.provider_profile_effective_at,
                    "requested_at": aidy.requested_as_of_utc,
                    "context_at": aidy.context_as_of_utc,
                    "lag": aidy.context_lag_seconds,
                    "context_hash": aidy.context_hash,
                    "snapshot_id": aidy.snapshot_id,
                    "snapshot_digest": aidy.snapshot_digest,
                    "snapshot_archive_key": aidy.snapshot_archive_key,
                    "session_json": _canonical(aidy.session),
                    "regime_json": _canonical(aidy.regime),
                    "data_quality_json": _canonical(aidy.data_quality),
                    "market_json": _canonical(aidy.market),
                    "provenance_json": _canonical(aidy.provenance),
                    "payload": _canonical(payload),
                    "digest": digest,
                    "contract_version": CONTRACT_VERSION,
                },
            ).scalar_one_or_none()
            session.commit()
        return inserted is not None

    def _persist_terminal_miss(
        self,
        candidate: ContextAttachmentCandidate,
        miss: AidyContextTerminalMiss,
    ) -> bool:
        if miss.reason != "pit_context_stale":
            raise ValueError("provider_context_terminal_miss_reason_invalid")
        response_payload = dict(miss.payload)
        response_payload["error"] = miss.reason
        with self._session_factory() as session:
            inserted = session.execute(
                text(
                    """
                    INSERT INTO provider_signal_context_terminal_misses(
                        id,signal_id,source_id,message_id,signal_posted_at,
                        reason,response_payload,contract_version,
                        research_only,live_money_execution_allowed
                    ) VALUES (
                        :id,:signal_id,:source_id,:message_id,:signal_posted_at,
                        :reason,CAST(:response_payload AS jsonb),:contract_version,
                        true,false
                    )
                    ON CONFLICT (signal_id) DO NOTHING
                    RETURNING id
                    """
                ),
                {
                    "id": uuid4(),
                    "signal_id": candidate.signal_id,
                    "source_id": candidate.source_id,
                    "message_id": candidate.message_id,
                    "signal_posted_at": candidate.signal_posted_at,
                    "reason": miss.reason,
                    "response_payload": _canonical(response_payload),
                    "contract_version": TERMINAL_MISS_CONTRACT_VERSION,
                },
            ).scalar_one_or_none()
            session.commit()
        return inserted is not None

    async def resolve_once(self) -> tuple[int, int]:
        attached = 0
        failures = 0
        self._last_terminal_misses = 0
        for candidate in self._candidates():
            try:
                joined = await join_provider_to_aidy_context(
                    client=self._client,
                    signal_posted_at=candidate.signal_posted_at,
                    provider_profile_pit_status="resolved",
                    provider_profile_version_id=str(candidate.provider_profile_version_id),
                    provider_profile_version_no=candidate.provider_profile_version_no,
                    provider_profile_effective_at=candidate.provider_profile_effective_at,
                )
                if self._persist(candidate, joined):
                    attached += 1
            except AidyContextTerminalMiss as exc:
                if self._persist_terminal_miss(candidate, exc):
                    self._last_terminal_misses += 1
                logger.info(
                    "Provider context terminal miss recorded signal_id=%s reason=%s",
                    candidate.signal_id,
                    exc.reason,
                )
            except ProviderContextJoinBlocked as exc:
                failures += 1
                logger.warning(
                    "Provider context attachment blocked signal_id=%s reason=%s",
                    candidate.signal_id,
                    str(exc),
                )
            except Exception:
                failures += 1
                logger.exception(
                    "Provider context attachment failed safely signal_id=%s",
                    candidate.signal_id,
                )
        return attached, failures


__all__ = [
    "CONTRACT_VERSION",
    "TERMINAL_MISS_CONTRACT_VERSION",
    "ContextAttachmentCandidate",
    "ProviderContextAttachmentResolver",
]
