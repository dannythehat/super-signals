"""Day 17 strict validation and append-only admin review evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app.models import AuditEvent, Message

VALIDATOR_VERSION = "day17-strict-v1"


@dataclass(frozen=True, slots=True)
class StrictValidationResult:
    status: str
    reason: str
    matched_rules: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReviewBackfillResult:
    validations_created: int
    review_items_created: int


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        result = value
    else:
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
    if not result.is_finite() or result <= 0:
        return None
    return result


def validate_parsed_trade(
    *,
    symbol: str | None,
    direction: str | None,
    entry_price: Any,
    stop_loss: Any,
    take_profits: list[Any] | tuple[Any, ...] | None,
    size_multiplier: Any,
) -> StrictValidationResult:
    """Validate parsed XAUUSD evidence without repairing or inferring values."""

    if symbol != "XAUUSD":
        return StrictValidationResult(
            "failed",
            "Validated trade must be XAUUSD for the current parser scope.",
            ("invalid_symbol",),
        )
    if direction not in {"BUY", "SELL"}:
        return StrictValidationResult(
            "failed",
            "Validated trade must contain exactly one BUY or SELL direction.",
            ("invalid_direction",),
        )

    entry = _decimal(entry_price)
    stop = _decimal(stop_loss)
    targets = tuple(_decimal(value) for value in (take_profits or []))
    multiplier = _decimal(size_multiplier)
    if entry is None:
        return StrictValidationResult("failed", "Entry price evidence is missing or invalid.", ("invalid_entry",))
    if stop is None:
        return StrictValidationResult("failed", "Stop-loss evidence is missing or invalid.", ("invalid_stop_loss",))
    if not targets or any(value is None for value in targets):
        return StrictValidationResult(
            "failed",
            "Take-profit evidence is missing or invalid.",
            ("invalid_take_profits",),
        )
    if multiplier not in {Decimal("1"), Decimal("2")}:
        return StrictValidationResult(
            "failed",
            "Size instruction must be the accepted normal or double-size value.",
            ("invalid_size_multiplier",),
        )

    concrete_targets = tuple(value for value in targets if value is not None)
    if direction == "BUY":
        if stop >= entry:
            return StrictValidationResult(
                "failed",
                "BUY stop loss must be strictly below entry.",
                ("buy_stop_not_below_entry",),
            )
        if any(target <= entry for target in concrete_targets):
            return StrictValidationResult(
                "failed",
                "Every BUY take-profit must be strictly above entry.",
                ("buy_target_not_above_entry",),
            )
        if any(right <= left for left, right in zip(concrete_targets, concrete_targets[1:])):
            return StrictValidationResult(
                "failed",
                "BUY take-profits must progress strictly upward from TP1.",
                ("buy_targets_not_increasing",),
            )
    else:
        if stop <= entry:
            return StrictValidationResult(
                "failed",
                "SELL stop loss must be strictly above entry.",
                ("sell_stop_not_above_entry",),
            )
        if any(target >= entry for target in concrete_targets):
            return StrictValidationResult(
                "failed",
                "Every SELL take-profit must be strictly below entry.",
                ("sell_target_not_below_entry",),
            )
        if any(right >= left for left, right in zip(concrete_targets, concrete_targets[1:])):
            return StrictValidationResult(
                "failed",
                "SELL take-profits must progress strictly downward from TP1.",
                ("sell_targets_not_decreasing",),
            )

    return StrictValidationResult(
        "valid",
        "Parsed XAUUSD trade passed strict directional validation.",
        (
            "xauusd_symbol",
            "direction",
            "entry",
            "stop_direction",
            "target_direction",
            "target_order",
            "accepted_size_multiplier",
        ),
    )


class MessageReviewService:
    """Create strict validation evidence and queue only stopped trade-like revisions."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def process_original(self, source_id: UUID, telegram_message_id: int) -> bool:
        with self._session_factory() as session:
            message = session.scalar(
                select(Message).where(
                    Message.source_id == source_id,
                    Message.telegram_message_id == telegram_message_id,
                )
            )
            if message is None or message.deleted_at is not None:
                return False
            changed = self._process_revision(
                session,
                message_id=message.id,
                revision_index=0,
                raw_text=message.raw_text,
            )
            session.commit()
            return changed

    def process_latest_revision(self, source_id: UUID, telegram_message_id: int) -> bool:
        with self._session_factory() as session:
            message = session.scalar(
                select(Message).where(
                    Message.source_id == source_id,
                    Message.telegram_message_id == telegram_message_id,
                )
            )
            if message is None or message.deleted_at is not None:
                return False
            revision = session.execute(
                text(
                    """
                    SELECT revision_index, raw_text
                    FROM message_revisions
                    WHERE message_id = :message_id
                    ORDER BY revision_index DESC
                    LIMIT 1
                    """
                ),
                {"message_id": message.id},
            ).mappings().first()
            if revision is None:
                return False
            changed = self._process_revision(
                session,
                message_id=message.id,
                revision_index=int(revision["revision_index"]),
                raw_text=str(revision["raw_text"] or ""),
            )
            session.commit()
            return changed

    def backfill(self) -> ReviewBackfillResult:
        validations_created = 0
        review_items_created = 0
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT
                        mc.message_id,
                        mc.revision_index,
                        CASE
                            WHEN mc.revision_index = 0 THEN m.raw_text
                            ELSE mr.raw_text
                        END AS raw_text
                    FROM message_classifications AS mc
                    JOIN messages AS m ON m.id = mc.message_id
                    LEFT JOIN message_revisions AS mr
                      ON mr.message_id = mc.message_id
                     AND mr.revision_index = mc.revision_index
                    WHERE m.deleted_at IS NULL
                    ORDER BY mc.created_at ASC, mc.revision_index ASC
                    """
                )
            ).mappings().all()
            for row in rows:
                before_validation = self._validation_exists(
                    session, row["message_id"], int(row["revision_index"])
                )
                before_review = self._review_exists(
                    session, row["message_id"], int(row["revision_index"])
                )
                self._process_revision(
                    session,
                    message_id=row["message_id"],
                    revision_index=int(row["revision_index"]),
                    raw_text=str(row["raw_text"] or ""),
                )
                if not before_validation and self._validation_exists(
                    session, row["message_id"], int(row["revision_index"])
                ):
                    validations_created += 1
                if not before_review and self._review_exists(
                    session, row["message_id"], int(row["revision_index"])
                ):
                    review_items_created += 1
            session.commit()
        return ReviewBackfillResult(validations_created, review_items_created)

    @staticmethod
    def _validation_exists(session: Session, message_id: UUID, revision_index: int) -> bool:
        return bool(
            session.execute(
                text(
                    """
                    SELECT 1 FROM message_validations
                    WHERE message_id = :message_id AND revision_index = :revision_index
                    """
                ),
                {"message_id": message_id, "revision_index": revision_index},
            ).scalar_one_or_none()
        )

    @staticmethod
    def _review_exists(session: Session, message_id: UUID, revision_index: int) -> bool:
        return bool(
            session.execute(
                text(
                    """
                    SELECT 1 FROM message_review_items
                    WHERE message_id = :message_id AND revision_index = :revision_index
                    """
                ),
                {"message_id": message_id, "revision_index": revision_index},
            ).scalar_one_or_none()
        )

    def _process_revision(
        self,
        session: Session,
        *,
        message_id: UUID,
        revision_index: int,
        raw_text: str,
    ) -> bool:
        row = session.execute(
            text(
                """
                SELECT
                    mc.classification,
                    mc.decision_status,
                    mc.reason AS classification_reason,
                    mc.matched_rules AS classification_rules,
                    mc.classifier_version,
                    mp.parse_status,
                    mp.reason AS parse_reason,
                    mp.matched_rules AS parse_rules,
                    mp.parser_version,
                    mp.symbol,
                    mp.direction,
                    mp.entry_price,
                    mp.stop_loss,
                    mp.take_profits,
                    mp.size_multiplier
                FROM message_classifications AS mc
                LEFT JOIN message_parses AS mp
                  ON mp.message_id = mc.message_id
                 AND mp.revision_index = mc.revision_index
                WHERE mc.message_id = :message_id
                  AND mc.revision_index = :revision_index
                """
            ),
            {"message_id": message_id, "revision_index": revision_index},
        ).mappings().first()
        if row is None:
            return False

        classification = str(row["classification"])
        decision_status = str(row["decision_status"])
        if classification == "uncertain" and decision_status == "review":
            return self._insert_review(
                session,
                message_id=message_id,
                revision_index=revision_index,
                raw_text=raw_text,
                review_stage="classification",
                reason=str(row["classification_reason"]),
                matched_rules=[str(item) for item in (row["classification_rules"] or [])],
                row=row,
            )

        if classification != "new_trade" or decision_status != "classified":
            return False

        parse_status = row["parse_status"]
        if parse_status is None:
            return False
        if str(parse_status) == "failed":
            return self._insert_review(
                session,
                message_id=message_id,
                revision_index=revision_index,
                raw_text=raw_text,
                review_stage="parser",
                reason=str(row["parse_reason"]),
                matched_rules=[str(item) for item in (row["parse_rules"] or [])],
                row=row,
            )

        validation = validate_parsed_trade(
            symbol=str(row["symbol"]) if row["symbol"] is not None else None,
            direction=str(row["direction"]) if row["direction"] is not None else None,
            entry_price=row["entry_price"],
            stop_loss=row["stop_loss"],
            take_profits=list(row["take_profits"] or []),
            size_multiplier=row["size_multiplier"],
        )
        validation_inserted = self._insert_validation(
            session,
            message_id=message_id,
            revision_index=revision_index,
            raw_text=raw_text,
            result=validation,
        )
        review_inserted = False
        if validation.status == "failed":
            review_inserted = self._insert_review(
                session,
                message_id=message_id,
                revision_index=revision_index,
                raw_text=raw_text,
                review_stage="validation",
                reason=validation.reason,
                matched_rules=list(validation.matched_rules),
                row=row,
            )
        return validation_inserted or review_inserted

    @staticmethod
    def _insert_validation(
        session: Session,
        *,
        message_id: UUID,
        revision_index: int,
        raw_text: str,
        result: StrictValidationResult,
    ) -> bool:
        inserted_id = session.execute(
            text(
                """
                INSERT INTO message_validations (
                    message_id,
                    revision_index,
                    validation_status,
                    reason,
                    matched_rules,
                    validated_text_sha256,
                    validator_version
                )
                VALUES (
                    :message_id,
                    :revision_index,
                    :validation_status,
                    :reason,
                    CAST(:matched_rules AS jsonb),
                    :validated_text_sha256,
                    :validator_version
                )
                ON CONFLICT (message_id, revision_index) DO NOTHING
                RETURNING id
                """
            ),
            {
                "message_id": message_id,
                "revision_index": revision_index,
                "validation_status": result.status,
                "reason": result.reason,
                "matched_rules": json.dumps(list(result.matched_rules)),
                "validated_text_sha256": sha256(raw_text.encode("utf-8")).hexdigest(),
                "validator_version": VALIDATOR_VERSION,
            },
        ).scalar_one_or_none()
        if inserted_id is None:
            return False
        session.add(
            AuditEvent(
                actor_user_id=None,
                event_type="message.validated",
                entity_type="message",
                entity_id=message_id,
                payload={
                    "revision_index": revision_index,
                    "validation_status": result.status,
                    "matched_rules": list(result.matched_rules),
                    "validator_version": VALIDATOR_VERSION,
                    "signal_created": False,
                    "position_created": False,
                    "lot_size_calculated": False,
                    "trade_action_created": False,
                },
            )
        )
        return True

    @staticmethod
    def _insert_review(
        session: Session,
        *,
        message_id: UUID,
        revision_index: int,
        raw_text: str,
        review_stage: str,
        reason: str,
        matched_rules: list[str],
        row: Any,
    ) -> bool:
        take_profits = [str(item) for item in (row["take_profits"] or [])]
        inserted_id = session.execute(
            text(
                """
                INSERT INTO message_review_items (
                    message_id,
                    revision_index,
                    review_stage,
                    review_status,
                    reason,
                    matched_rules,
                    raw_text,
                    raw_text_sha256,
                    classification,
                    decision_status,
                    classifier_version,
                    parse_status,
                    parser_version,
                    validator_version,
                    symbol,
                    direction,
                    entry_price,
                    stop_loss,
                    take_profits,
                    size_multiplier
                )
                VALUES (
                    :message_id,
                    :revision_index,
                    :review_stage,
                    'open',
                    :reason,
                    CAST(:matched_rules AS jsonb),
                    :raw_text,
                    :raw_text_sha256,
                    :classification,
                    :decision_status,
                    :classifier_version,
                    :parse_status,
                    :parser_version,
                    :validator_version,
                    :symbol,
                    :direction,
                    :entry_price,
                    :stop_loss,
                    CAST(:take_profits AS jsonb),
                    :size_multiplier
                )
                ON CONFLICT (message_id, revision_index) DO NOTHING
                RETURNING id
                """
            ),
            {
                "message_id": message_id,
                "revision_index": revision_index,
                "review_stage": review_stage,
                "reason": reason,
                "matched_rules": json.dumps(matched_rules),
                "raw_text": raw_text,
                "raw_text_sha256": sha256(raw_text.encode("utf-8")).hexdigest(),
                "classification": row["classification"],
                "decision_status": row["decision_status"],
                "classifier_version": row["classifier_version"],
                "parse_status": row["parse_status"],
                "parser_version": row["parser_version"],
                "validator_version": VALIDATOR_VERSION if review_stage == "validation" else None,
                "symbol": row["symbol"],
                "direction": row["direction"],
                "entry_price": row["entry_price"],
                "stop_loss": row["stop_loss"],
                "take_profits": json.dumps(take_profits),
                "size_multiplier": row["size_multiplier"],
            },
        ).scalar_one_or_none()
        if inserted_id is None:
            return False
        session.add(
            AuditEvent(
                actor_user_id=None,
                event_type="message.review_queued",
                entity_type="message",
                entity_id=message_id,
                payload={
                    "revision_index": revision_index,
                    "review_stage": review_stage,
                    "matched_rules": matched_rules,
                    "validator_version": VALIDATOR_VERSION if review_stage == "validation" else None,
                    "signal_created": False,
                    "position_created": False,
                    "lot_size_calculated": False,
                    "trade_action_created": False,
                },
            )
        )
        return True
