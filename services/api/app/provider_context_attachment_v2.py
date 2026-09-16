"""Provider Context v2 recovery adapter.

The v1 terminal-miss row is audit history, not a tombstone forever. After AIDY gained
an explicitly degraded D1-only context grade, v1 misses may be retried once under this
new contract. Broader/still-stale misses are persisted as v2 and then remain terminal.
No broker, routing or live-money path is imported here.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import text

from app.aidy_context_client import AidyContextTerminalMiss
from app.provider_context_attachment import (
    ContextAttachmentCandidate,
    ProviderContextAttachmentResolver,
)

TERMINAL_MISS_CONTRACT_VERSION_V2 = "provider_aidy_context_terminal_miss_v2"


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class ProviderContextAttachmentResolverV2(ProviderContextAttachmentResolver):
    """Replay v1 misses once under the v2 D1-only degraded-context contract."""

    def _candidates(self) -> list[ContextAttachmentCandidate]:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT DISTINCT ON (t.signal_id)
                           t.signal_id,m.source_id AS source_id,
                           s.source_message_id AS message_id,
                           s.source_posted_at AS signal_posted_at,
                           t.provider_profile_version_id,t.provider_profile_version_no,
                           t.provider_profile_effective_at
                    FROM shadow_trades t
                    JOIN signals s ON s.id=t.signal_id
                    JOIN messages m ON m.id=s.source_message_id
                    WHERE t.provider_profile_pit_status='resolved'
                      AND t.provider_profile_version_id IS NOT NULL
                      AND t.source_id=m.source_id
                      AND t.message_id=s.source_message_id
                      AND t.provider_profile_effective_at <= s.source_posted_at
                      AND NOT EXISTS (
                          SELECT 1
                          FROM provider_signal_context_attachments a
                          WHERE a.signal_id=t.signal_id
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM provider_signal_context_terminal_misses x
                          WHERE x.signal_id=t.signal_id
                            AND x.contract_version=:contract_version
                      )
                    ORDER BY t.signal_id,t.entry_index
                    LIMIT :limit
                    """
                ),
                {
                    "limit": self._batch_limit,
                    "contract_version": TERMINAL_MISS_CONTRACT_VERSION_V2,
                },
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
                    signal_posted_at=posted_at.astimezone(UTC),
                    provider_profile_version_id=UUID(str(row["provider_profile_version_id"])),
                    provider_profile_version_no=int(row["provider_profile_version_no"]),
                    provider_profile_effective_at=profile_at.astimezone(UTC),
                )
            )
        return candidates

    def _persist_terminal_miss(
        self,
        candidate: ContextAttachmentCandidate,
        miss: AidyContextTerminalMiss,
    ) -> bool:
        if miss.reason != "pit_context_stale":
            raise ValueError("provider_context_terminal_miss_reason_invalid")
        response_payload = dict(miss.payload)
        response_payload["error"] = miss.reason
        response_payload["replay_contract"] = TERMINAL_MISS_CONTRACT_VERSION_V2
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
                    ON CONFLICT (signal_id,contract_version) DO NOTHING
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
                    "contract_version": TERMINAL_MISS_CONTRACT_VERSION_V2,
                },
            ).scalar_one_or_none()
            session.commit()
        return inserted is not None


__all__ = [
    "TERMINAL_MISS_CONTRACT_VERSION_V2",
    "ProviderContextAttachmentResolverV2",
]
