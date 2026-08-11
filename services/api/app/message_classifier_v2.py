"""Versioned classifier corrections learned from real Testing sources.

The original Day 15 classifier remains immutable historical behaviour. This
version keeps its conservative rules but fixes one real-world edge case:
providers sometimes post a complete new trade as a Telegram reply. Reply context
alone must not turn a structurally complete trade into an ambiguous update.
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.message_classifier import (
    ClassificationResult,
    MessageClassificationService,
    classify_message,
)
from app.models import AuditEvent

CLASSIFIER_VERSION_V2 = "day15-v2-real-source-formats"


def classify_message_v2(
    raw_text: str,
    *,
    reply_to_message_id: int | None = None,
) -> ClassificationResult:
    """Apply Day 15 rules plus the real-source reply correction."""

    result = classify_message(raw_text, reply_to_message_id=reply_to_message_id)
    if (
        result.classification == "uncertain"
        and result.decision_status == "review"
        and len(result.matched_rules) == 2
        and result.matched_rules[0] == "new_trade_structure"
        and result.matched_rules[1] == "reply_management"
    ):
        return ClassificationResult(
            classification="new_trade",
            decision_status="classified",
            reason=(
                "Message contains complete new-trade structure; Telegram reply "
                "context alone does not convert it into a trade update."
            ),
            matched_rules=("new_trade_structure", "reply_context_ignored"),
        )
    return result


class MessageClassificationServiceV2(MessageClassificationService):
    """Persist new classifications with the v2 classifier version."""

    @staticmethod
    def _insert_classification(
        session: Any,
        *,
        message_id: UUID,
        revision_index: int,
        raw_text: str,
        reply_to_message_id: int | None,
    ) -> bool:
        result = classify_message_v2(
            raw_text,
            reply_to_message_id=reply_to_message_id,
        )
        content_hash = sha256(raw_text.encode("utf-8")).hexdigest()
        inserted_id = session.execute(
            text(
                """
                INSERT INTO message_classifications (
                    message_id,
                    revision_index,
                    classification,
                    decision_status,
                    reason,
                    matched_rules,
                    classified_text_sha256,
                    classifier_version
                )
                VALUES (
                    :message_id,
                    :revision_index,
                    :classification,
                    :decision_status,
                    :reason,
                    CAST(:matched_rules AS jsonb),
                    :classified_text_sha256,
                    :classifier_version
                )
                ON CONFLICT (message_id, revision_index) DO NOTHING
                RETURNING id
                """
            ),
            {
                "message_id": message_id,
                "revision_index": revision_index,
                "classification": result.classification,
                "decision_status": result.decision_status,
                "reason": result.reason,
                "matched_rules": json.dumps(list(result.matched_rules)),
                "classified_text_sha256": content_hash,
                "classifier_version": CLASSIFIER_VERSION_V2,
            },
        ).scalar_one_or_none()
        if inserted_id is None:
            return False

        session.add(
            AuditEvent(
                actor_user_id=None,
                event_type="message.classified",
                entity_type="message",
                entity_id=message_id,
                payload={
                    "revision_index": revision_index,
                    "classification": result.classification,
                    "decision_status": result.decision_status,
                    "matched_rules": list(result.matched_rules),
                    "classifier_version": CLASSIFIER_VERSION_V2,
                    "signal_created": False,
                    "position_created": False,
                    "trade_action_created": False,
                },
            )
        )
        return True
