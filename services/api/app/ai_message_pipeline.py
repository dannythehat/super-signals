"""Immediate AI-authoritative Telegram decision pipeline base plumbing.

Every Testing/Live message and meaningful text edit receives one automatic decision.
Exact duplicate-text edits and textless/media-only posts are recorded deterministically.
OpenAI provides semantic interpretation; the current-message V1 mechanical policy is the
final authority for what may become executable.

Legacy message_classifications/message_parses may remain as historical ingestion data,
but this module contains no code capable of turning those old parse rows into a trade,
management action or execution fallback.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.ai_canonical_signal import AiCanonicalSignalService
from app.ai_lifecycle_bridge import AiLifecycleBridge
from app.ai_message_supervisor import (
    AiMessageDecision,
    AiSupervisorError,
    OpenAiMessageSupervisor,
)
from app.v1_message_policy import apply_v1_message_policy


@dataclass(frozen=True, slots=True)
class AiPipelineResult:
    decided: bool
    decision: str | None
    action: str | None
    signal_id: UUID | None
    lifecycle_event_id: UUID | None
    decision_source: str | None
    reason: str


class AiMessagePipeline:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        supervisor: OpenAiMessageSupervisor | None,
    ) -> None:
        self._session_factory = session_factory
        self._supervisor = supervisor
        self._signals = AiCanonicalSignalService(session_factory)
        self._lifecycle = AiLifecycleBridge(session_factory)

    def process_original(self, source_id: UUID, telegram_message_id: int) -> AiPipelineResult:
        return self._process_revision(source_id, telegram_message_id, revision_index=0)

    def process_latest_revision(
        self,
        source_id: UUID,
        telegram_message_id: int,
    ) -> AiPipelineResult:
        with self._session_factory() as session:
            revision_index = session.execute(
                text(
                    """
                    SELECT COALESCE(MAX(mr.revision_index), 0)
                    FROM messages AS m
                    LEFT JOIN message_revisions AS mr ON mr.message_id = m.id
                    WHERE m.source_id = :source_id
                      AND m.telegram_message_id = :telegram_message_id
                    """
                ),
                {"source_id": source_id, "telegram_message_id": telegram_message_id},
            ).scalar_one_or_none()
        if revision_index is None or int(revision_index) <= 0:
            return AiPipelineResult(False, None, None, None, None, None, "revision_not_found")
        return self._process_revision(
            source_id,
            telegram_message_id,
            revision_index=int(revision_index),
        )

    def _process_revision(
        self,
        source_id: UUID,
        telegram_message_id: int,
        *,
        revision_index: int,
    ) -> AiPipelineResult:
        with self._session_factory() as session:
            row = self._load_revision(
                session,
                source_id=source_id,
                telegram_message_id=telegram_message_id,
                revision_index=revision_index,
            )
            if row is None:
                return AiPipelineResult(
                    False, None, None, None, None, None, "message_not_eligible"
                )

            existing = session.execute(
                text(
                    """
                    SELECT decision, action, decision_source
                    FROM ai_message_decisions
                    WHERE message_id = :message_id
                      AND revision_index = :revision_index
                    """
                ),
                {"message_id": row["message_id"], "revision_index": revision_index},
            ).mappings().first()
            if existing is not None:
                return AiPipelineResult(
                    True,
                    str(existing["decision"]),
                    str(existing["action"]),
                    None,
                    None,
                    str(existing["decision_source"]),
                    "already_decided",
                )

            reply_context = self._reply_context(session, source_id, row["raw_payload"])
            previous_text = self._previous_text(
                session, row["message_id"], revision_index
            )
            existing_signal_id = session.execute(
                text(
                    "SELECT id FROM signals "
                    "WHERE source_message_id = :message_id LIMIT 1"
                ),
                {"message_id": row["message_id"]},
            ).scalar_one_or_none()
            execution_started = False
            if existing_signal_id is not None:
                execution_started = bool(
                    session.execute(
                        text(
                            """
                            SELECT EXISTS(
                                SELECT 1 FROM positions WHERE signal_id = :signal_id
                            )
                            """
                        ),
                        {"signal_id": existing_signal_id},
                    ).scalar_one()
                )

        raw_text = str(row["raw_text"] or "")

        if revision_index > 0 and previous_text is not None and raw_text == previous_text:
            decision = self._non_actionable_without_ai(
                raw_text,
                reason="identical_text_revision",
            )
            self._store_decision(row["message_id"], revision_index, decision)
            return AiPipelineResult(
                True,
                decision.decision,
                decision.action,
                existing_signal_id,
                None,
                decision.source,
                decision.reason,
            )

        if not raw_text.strip():
            payload = row["raw_payload"] if isinstance(row["raw_payload"], dict) else {}
            reason = (
                "media_only_requires_media_ingestion"
                if bool(payload.get("has_media"))
                else "empty_message"
            )
            decision = self._non_actionable_without_ai(raw_text, reason=reason)
            self._store_decision(row["message_id"], revision_index, decision)
            return AiPipelineResult(
                True,
                decision.decision,
                decision.action,
                existing_signal_id,
                None,
                decision.source,
                decision.reason,
            )

        decision = self._decide(
            source_id=source_id,
            telegram_message_id=telegram_message_id,
            revision_index=revision_index,
            raw_text=raw_text,
            source_status=str(row["source_status"]),
            reply_context=reply_context,
            previous_text=previous_text,
        )

        if revision_index > 0 and existing_signal_id is not None:
            if execution_started:
                decision = replace(
                    decision,
                    decision="non_actionable",
                    action="skip",
                    reason="post_execution_edit",
                )
            else:
                decision = replace(
                    decision,
                    decision="new_trade",
                    action="execute",
                )

        decision = apply_v1_message_policy(
            decision,
            raw_text=raw_text,
            is_edit=revision_index > 0,
            original_has_signal=existing_signal_id is not None,
            previous_text=previous_text,
        )
        self._store_decision(row["message_id"], revision_index, decision)

        signal_id: UUID | None = None
        lifecycle_event_id: UUID | None = None
        dispatch_reason = decision.reason

        if revision_index > 0 and existing_signal_id is not None:
            if execution_started:
                signal_id = existing_signal_id
                dispatch_reason = "post_execution_edit"
            else:
                revision_result = self._signals.apply_pre_execution_revision(
                    message_id=row["message_id"],
                    extracted=decision.extracted,
                    revision_index=revision_index,
                    execute=(
                        decision.decision == "new_trade"
                        and decision.action == "execute"
                    ),
                    reason=decision.reason,
                )
                signal_id = revision_result.signal_id
                dispatch_reason = revision_result.reason
        elif decision.decision == "new_trade" and decision.action == "execute":
            signal_result = self._signals.process(
                message_id=row["message_id"],
                extracted=decision.extracted,
                revision_index=revision_index,
            )
            signal_id = signal_result.signal_id
            dispatch_reason = signal_result.reason
        elif (
            decision.decision == "trade_update"
            and decision.action == "apply_update"
        ):
            lifecycle_result = self._lifecycle.process(
                message_id=row["message_id"],
                extracted=decision.extracted,
                revision_index=revision_index,
            )
            signal_id = lifecycle_result.signal_id
            lifecycle_event_id = lifecycle_result.event_id
            dispatch_reason = lifecycle_result.reason

        return AiPipelineResult(
            True,
            decision.decision,
            decision.action,
            signal_id,
            lifecycle_event_id,
            decision.source,
            dispatch_reason,
        )

    @staticmethod
    def _non_actionable_without_ai(raw_text: str, *, reason: str) -> AiMessageDecision:
        return AiMessageDecision(
            decision="non_actionable",
            action="skip",
            confidence=1.0,
            reason=reason,
            extracted={},
            model="deterministic-no-ai-v1",
            response_id=None,
            latency_ms=0,
            source="deterministic_no_ai",
            raw_text_sha256=sha256(raw_text.encode("utf-8")).hexdigest(),
        )

    @staticmethod
    def _load_revision(
        session: Session,
        *,
        source_id: UUID,
        telegram_message_id: int,
        revision_index: int,
    ) -> Any | None:
        return session.execute(
            text(
                """
                SELECT
                    m.id AS message_id,
                    CASE WHEN :revision_index = 0
                         THEN m.raw_text ELSE mr.raw_text END AS raw_text,
                    CASE WHEN :revision_index = 0
                         THEN m.raw_payload ELSE mr.raw_payload END AS raw_payload,
                    s.status AS source_status
                FROM messages AS m
                JOIN sources AS s ON s.id = m.source_id
                LEFT JOIN message_revisions AS mr
                  ON mr.message_id = m.id
                 AND mr.revision_index = :revision_index
                WHERE m.source_id = :source_id
                  AND m.telegram_message_id = :telegram_message_id
                  AND m.deleted_at IS NULL
                  AND s.status IN ('testing', 'live')
                  AND (:revision_index = 0 OR mr.revision_index IS NOT NULL)
                """
            ),
            {
                "source_id": source_id,
                "telegram_message_id": telegram_message_id,
                "revision_index": revision_index,
            },
        ).mappings().first()

    def _decide(
        self,
        *,
        source_id: UUID,
        telegram_message_id: int,
        revision_index: int,
        raw_text: str,
        source_status: str,
        reply_context: str | None,
        previous_text: str | None,
    ) -> AiMessageDecision:
        del source_id, telegram_message_id
        if self._supervisor is not None:
            try:
                return self._supervisor.decide(
                    raw_text=raw_text,
                    source_status=source_status,
                    reply_context=reply_context,
                    previous_text=previous_text,
                    is_edit=revision_index > 0,
                )
            except AiSupervisorError:
                pass
        return self._non_actionable_without_ai(
            raw_text,
            reason="semantic_supervisor_unavailable_no_legacy_trade_fallback",
        )

    def _store_decision(
        self,
        message_id: UUID,
        revision_index: int,
        decision: AiMessageDecision,
    ) -> None:
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    INSERT INTO ai_message_decisions (
                        message_id,
                        revision_index,
                        decision,
                        action,
                        confidence,
                        reason,
                        extracted,
                        model,
                        response_id,
                        latency_ms,
                        decision_source,
                        raw_text_sha256
                    )
                    VALUES (
                        :message_id,
                        :revision_index,
                        :decision,
                        :action,
                        :confidence,
                        :reason,
                        CAST(:extracted AS jsonb),
                        :model,
                        :response_id,
                        :latency_ms,
                        :decision_source,
                        :raw_text_sha256
                    )
                    ON CONFLICT (message_id, revision_index) DO NOTHING
                    """
                ),
                {
                    "message_id": message_id,
                    "revision_index": revision_index,
                    "decision": decision.decision,
                    "action": decision.action,
                    "confidence": decision.confidence,
                    "reason": decision.reason,
                    "extracted": json.dumps(decision.extracted),
                    "model": decision.model,
                    "response_id": decision.response_id,
                    "latency_ms": decision.latency_ms,
                    "decision_source": decision.source,
                    "raw_text_sha256": decision.raw_text_sha256,
                },
            )
            session.commit()

    @staticmethod
    def _reply_context(
        session: Session,
        source_id: UUID,
        payload: Any,
    ) -> str | None:
        if not isinstance(payload, dict):
            return None
        reply_value = payload.get("reply_to_message_id")
        try:
            reply_id = int(reply_value) if reply_value is not None else None
        except (TypeError, ValueError):
            reply_id = None
        if reply_id is None:
            return None
        return session.execute(
            text(
                """
                SELECT raw_text
                FROM messages
                WHERE source_id = :source_id
                  AND telegram_message_id = :telegram_message_id
                  AND deleted_at IS NULL
                LIMIT 1
                """
            ),
            {"source_id": source_id, "telegram_message_id": reply_id},
        ).scalar_one_or_none()

    @staticmethod
    def _previous_text(
        session: Session,
        message_id: UUID,
        revision_index: int,
    ) -> str | None:
        if revision_index <= 0:
            return None
        if revision_index == 1:
            return session.execute(
                text("SELECT raw_text FROM messages WHERE id = :message_id"),
                {"message_id": message_id},
            ).scalar_one_or_none()
        return session.execute(
            text(
                """
                SELECT raw_text
                FROM message_revisions
                WHERE message_id = :message_id
                  AND revision_index = :revision_index
                """
            ),
            {
                "message_id": message_id,
                "revision_index": revision_index - 1,
            },
        ).scalar_one_or_none()
