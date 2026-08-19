"""Conservative Telegram message classification.

The classifier has three business categories: New Trade, Trade Update and Chatter.
``uncertain`` is an internal safety route to semantic interpretation, not a fourth
business category. Classification never creates a Signal, Position or broker action.

Observed provider shorthand such as ``I'm buying 4397`` or TGC's ``Im seling 4390``
is deliberately trade-looking even before SL/TP arrive. Those messages must reach the
semantic pipeline and may never be discarded as chatter simply because the provider
omitted the instrument token or misspelled SELLING.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app.models import AuditEvent, Message

CLASSIFIER_VERSION = "day15-v1"


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    classification: str
    decision_status: str
    reason: str
    matched_rules: tuple[str, ...]


_DIRECTION_BUY = re.compile(r"\bBUY(?:ING)?\b", re.IGNORECASE)
_DIRECTION_SELL = re.compile(r"\b(?:SELL(?:S|ING)?|SELING|SELLIMG)\b", re.IGNORECASE)
_INSTRUMENT = re.compile(
    r"\b(?:"
    r"XAUUSD|XAGUSD|GOLD|SILVER|"
    r"BTC(?:USD|USDT)?|ETH(?:USD|USDT)?|SOL(?:USD|USDT)?|"
    r"(?:EUR|GBP|USD|JPY|CHF|CAD|AUD|NZD)[/]?(?:EUR|GBP|USD|JPY|CHF|CAD|AUD|NZD)"
    r")\b",
    re.IGNORECASE,
)
_ENTRY_MARKER = re.compile(
    r"(?:\bENTRY\b|\bENTER\b|@\s*\d|\b(?:BUY|SELL)\s+(?:NOW|LIMIT|STOP)\b)",
    re.IGNORECASE,
)
_SL_MARKER = re.compile(r"\b(?:SL|STOP\s*LOSS)\b", re.IGNORECASE)
_TP_MARKER = re.compile(r"\b(?:TP\s*\d*|TAKE\s*PROFIT)\b", re.IGNORECASE)
_NUMBER = re.compile(r"(?<![A-Z])\d+(?:[.,]\d+)?", re.IGNORECASE)
_PRESENT_TENSE_NUMERIC_ENTRY = re.compile(
    r"\b(?:I\s*['’]?\s*M|I\s+AM)\s+"
    r"(?:BUYING|SELLING|SELING|SELLIMG)\b"
    r"(?:\s+(?:NOW|IF\s+WE\s+TAP(?:\s+IT)?))?"
    r"[^\d]{0,24}\d+(?:[.,]\d+)?\b",
    re.IGNORECASE,
)

_UPDATE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("move_stop", re.compile(r"\bMOVE\s+(?:SL|STOP(?:\s+LOSS)?)\b", re.IGNORECASE)),
    (
        "break_even",
        re.compile(
            r"\b(?:SL|STOP(?:\s+LOSS)?)\s+(?:TO\s+)?(?:BE|BREAK\s*EVEN)\b|\bBREAK\s*EVEN\b",
            re.IGNORECASE,
        ),
    ),
    (
        "close_trade",
        re.compile(
            r"\bCLOSE(?:\s+(?:NOW|TRADE|POSITION|PARTIAL|HALF|\d+\s*%))?\b|\bPARTIAL\s+CLOSE\b",
            re.IGNORECASE,
        ),
    ),
    (
        "cancel_order",
        re.compile(r"\bCANCEL(?:\s+(?:ORDER|TRADE|PENDING))?\b", re.IGNORECASE),
    ),
    (
        "target_hit",
        re.compile(
            r"\b(?:TP\s*#?\s*\d*|TARGET\s*#?\s*\d+)\s+(?:HIT|REACHED|DONE)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "stop_hit",
        re.compile(r"\b(?:SL|STOP\s*LOSS)\s+(?:HIT|REACHED)\b", re.IGNORECASE),
    ),
    (
        "secure_profit",
        re.compile(
            r"\bSECURE\s+PROFITS?\b|\bTAKE\s+(?:PARTIAL\s+)?PROFIT\s+NOW\b",
            re.IGNORECASE,
        ),
    ),
    (
        "hold_existing_trade",
        re.compile(
            r"\bHOLD\s+(?:THE\s+)?(?:TRADE|POSITION)\b|\bKEEP\s+(?:IT\s+)?RUNNING\b",
            re.IGNORECASE,
        ),
    ),
)

_CHATTER_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "greeting",
        re.compile(r"\b(?:GOOD\s+(?:MORNING|AFTERNOON|EVENING|NIGHT)|HELLO|HI\s+(?:TEAM|FAMILY|GUYS))\b", re.IGNORECASE),
    ),
    (
        "promotion",
        re.compile(r"\b(?:JOIN\s+(?:VIP|PREMIUM)|SUBSCRIBE|DISCOUNT|SIGN\s*UP)\b", re.IGNORECASE),
    ),
    (
        "general_commentary",
        re.compile(
            r"\b(?:LOOKS?\s+(?:BULLISH|BEARISH)|WATCHING|ANALYSIS|MARKET\s+UPDATE|NEWS|SETUP\s+FORMING)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "performance_chatter",
        re.compile(
            r"\b(?:PROFITS?|RESULTS?|FIRST\s+TARGETS?|GREAT\s+START|WHAT\s+A\s+DAY|SIMPLE\s+AS\s+THAT)\b",
            re.IGNORECASE,
        ),
    ),
)


def _normalise(raw_text: str) -> str:
    return " ".join((raw_text or "").replace("\u00a0", " ").split())


def classify_message(
    raw_text: str,
    *,
    reply_to_message_id: int | None = None,
) -> ClassificationResult:
    """Classify text conservatively without extracting trade fields."""

    normalised = _normalise(raw_text)
    if not normalised:
        return ClassificationResult(
            classification="chatter",
            decision_status="ignored",
            reason="Message contains no text to classify.",
            matched_rules=("empty_text",),
        )

    has_buy = bool(_DIRECTION_BUY.search(normalised))
    has_sell = bool(_DIRECTION_SELL.search(normalised))
    has_direction = has_buy or has_sell
    has_instrument = bool(_INSTRUMENT.search(normalised))
    has_entry = bool(_ENTRY_MARKER.search(normalised))
    has_sl = bool(_SL_MARKER.search(normalised))
    has_tp = bool(_TP_MARKER.search(normalised))
    number_count = len(_NUMBER.findall(normalised))
    present_tense_numeric_entry = bool(_PRESENT_TENSE_NUMERIC_ENTRY.search(normalised))

    update_rules = tuple(name for name, pattern in _UPDATE_RULES if pattern.search(normalised))
    chatter_rules = tuple(name for name, pattern in _CHATTER_RULES if pattern.search(normalised))

    reply_update_hint = bool(
        reply_to_message_id is not None
        and re.search(
            r"\b(?:SL|STOP|TP|TARGET|CLOSE|CANCEL|HOLD|PROFIT|BE|BREAK\s*EVEN)\b",
            normalised,
            re.IGNORECASE,
        )
    )
    if reply_update_hint and "reply_management" not in update_rules:
        update_rules = (*update_rules, "reply_management")

    strong_update = bool(update_rules)
    strong_new_trade = bool(
        has_direction
        and (
            (has_sl and has_tp)
            or (
                has_instrument
                and has_entry
                and (has_sl or has_tp)
                and number_count >= 1
            )
        )
    )

    if has_buy and has_sell:
        return ClassificationResult(
            classification="uncertain",
            decision_status="review",
            reason="Both BUY and SELL appear in the same message; classification is blocked.",
            matched_rules=("conflicting_directions",),
        )

    if strong_update and strong_new_trade:
        return ClassificationResult(
            classification="uncertain",
            decision_status="review",
            reason="Message contains both new-trade structure and trade-update instructions.",
            matched_rules=("new_trade_structure", *update_rules),
        )

    if strong_update:
        return ClassificationResult(
            classification="trade_update",
            decision_status="classified",
            reason="Clear existing-trade management or lifecycle language was found.",
            matched_rules=update_rules,
        )

    if strong_new_trade:
        rules: list[str] = ["direction"]
        if has_instrument:
            rules.append("instrument")
        if has_entry:
            rules.append("entry")
        if has_sl:
            rules.append("stop_loss")
        if has_tp:
            rules.append("take_profit")
        return ClassificationResult(
            classification="new_trade",
            decision_status="classified",
            reason="Message contains a clear BUY/SELL instruction with sufficient trade structure.",
            matched_rules=tuple(rules),
        )

    # Present-tense numeric entry language is a provider action candidate even when the
    # post omits the instrument/SL/TP. It must reach the semantic resolver rather than
    # being irreversibly labelled chatter. The execution gate still requires a complete,
    # source-valid trade before any broker mutation.
    if present_tense_numeric_entry:
        return ClassificationResult(
            classification="uncertain",
            decision_status="review",
            reason="Present-tense numeric BUY/SELL entry must reach semantic interpretation.",
            matched_rules=("present_tense_numeric_entry",),
        )

    trade_looking = bool(has_direction or has_entry or has_sl or has_tp)
    if trade_looking:
        rules: list[str] = []
        if has_direction:
            rules.append("direction_only_or_incomplete")
        if has_instrument:
            rules.append("instrument")
        if has_entry:
            rules.append("entry_marker")
        if has_sl:
            rules.append("stop_marker")
        if has_tp:
            rules.append("target_marker")
        return ClassificationResult(
            classification="uncertain",
            decision_status="review",
            reason="Trade-looking message is incomplete or too weak to classify safely.",
            matched_rules=tuple(rules) or ("weak_trade_intent",),
        )

    return ClassificationResult(
        classification="chatter",
        decision_status="ignored",
        reason="No actionable new-trade or existing-trade management instruction was found.",
        matched_rules=chatter_rules or ("non_actionable_text",),
    )


class MessageClassificationService:
    """Persist append-only classification evidence."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def classify_original(self, source_id: UUID, telegram_message_id: int) -> bool:
        with self._session_factory() as session:
            message = session.scalar(
                select(Message).where(
                    Message.source_id == source_id,
                    Message.telegram_message_id == telegram_message_id,
                )
            )
            if message is None or message.deleted_at is not None:
                return False
            reply_id = self._reply_id(message.raw_payload)
            inserted = self._insert_classification(
                session,
                message_id=message.id,
                revision_index=0,
                raw_text=message.raw_text,
                reply_to_message_id=reply_id,
            )
            session.commit()
            return inserted

    def classify_latest_revision(self, source_id: UUID, telegram_message_id: int) -> bool:
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
                    SELECT revision_index, raw_text, raw_payload
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
            inserted = self._insert_classification(
                session,
                message_id=message.id,
                revision_index=int(revision["revision_index"]),
                raw_text=str(revision["raw_text"] or ""),
                reply_to_message_id=self._reply_id(revision["raw_payload"]),
            )
            session.commit()
            return inserted

    def backfill_unclassified(self) -> int:
        inserted_count = 0
        with self._session_factory() as session:
            messages = session.scalars(
                select(Message)
                .where(Message.deleted_at.is_(None))
                .order_by(Message.created_at.asc(), Message.id.asc())
            ).all()
            for message in messages:
                if self._insert_classification(
                    session,
                    message_id=message.id,
                    revision_index=0,
                    raw_text=message.raw_text,
                    reply_to_message_id=self._reply_id(message.raw_payload),
                ):
                    inserted_count += 1
                revisions = session.execute(
                    text(
                        """
                        SELECT revision_index, raw_text, raw_payload
                        FROM message_revisions
                        WHERE message_id = :message_id
                        ORDER BY revision_index ASC
                        """
                    ),
                    {"message_id": message.id},
                ).mappings()
                for revision in revisions:
                    if self._insert_classification(
                        session,
                        message_id=message.id,
                        revision_index=int(revision["revision_index"]),
                        raw_text=str(revision["raw_text"] or ""),
                        reply_to_message_id=self._reply_id(revision["raw_payload"]),
                    ):
                        inserted_count += 1
            session.commit()
        return inserted_count

    @staticmethod
    def _reply_id(payload: Any) -> int | None:
        if not isinstance(payload, dict):
            return None
        value = payload.get("reply_to_message_id")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _insert_classification(
        session: Session,
        *,
        message_id: UUID,
        revision_index: int,
        raw_text: str,
        reply_to_message_id: int | None,
    ) -> bool:
        result = classify_message(raw_text, reply_to_message_id=reply_to_message_id)
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
                "classifier_version": CLASSIFIER_VERSION,
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
                    "classifier_version": CLASSIFIER_VERSION,
                    "signal_created": False,
                    "position_created": False,
                    "trade_action_created": False,
                },
            )
        )
        return True
