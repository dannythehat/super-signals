"""Day 21 read-only Telegram end-to-end acceptance gate.

The gate evaluates stored PostgreSQL evidence for one logical Telegram source.
It never changes a Signal, Position, publication, reader session, or broker state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class GateCheck:
    key: str
    status: str
    observed: int
    expected: str
    detail: str


@dataclass(frozen=True, slots=True)
class TelegramE2EGateReport:
    source_id: UUID
    source_alias: str
    source_status: str
    status: str
    generated_at: datetime
    checks: tuple[GateCheck, ...]


def _count(session: Session, sql: str, source_id: UUID) -> int:
    return int(
        session.execute(text(sql), {"source_id": source_id}).scalar_one()
    )


def _check(
    *,
    key: str,
    evidence_count: int,
    problem_count: int,
    expected: str,
    pass_detail: str,
    pending_detail: str,
    fail_detail: str,
) -> GateCheck:
    if problem_count:
        return GateCheck(key, "failed", problem_count, expected, fail_detail)
    if evidence_count == 0:
        return GateCheck(key, "pending", 0, expected, pending_detail)
    return GateCheck(key, "passed", evidence_count, expected, pass_detail)


def evaluate_telegram_e2e_gate(
    session: Session,
    source_id: UUID,
) -> TelegramE2EGateReport:
    """Return a deterministic Day 21 gate report for one selected source."""

    source = session.execute(
        text(
            """
            SELECT id, COALESCE(source_alias, chat_title, 'Telegram source') AS source_alias, status
            FROM sources
            WHERE id = :source_id
            """
        ),
        {"source_id": source_id},
    ).mappings().first()
    if source is None:
        raise LookupError("Telegram source was not found.")

    signal_count = _count(
        session,
        "SELECT COUNT(*) FROM signals WHERE source_id = :source_id",
        source_id,
    )
    root_publication_problems = _count(
        session,
        """
        SELECT COUNT(*)
        FROM (
            SELECT
                sig.id,
                COUNT(pub.id) FILTER (WHERE pub.publication_kind = 'signal_created') AS roots,
                COUNT(pub.id) FILTER (
                    WHERE pub.publication_kind = 'signal_created' AND pub.status = 'sent'
                ) AS sent_roots
            FROM signals AS sig
            LEFT JOIN telegram_publications AS pub ON pub.signal_id = sig.id
            WHERE sig.source_id = :source_id
            GROUP BY sig.id
        ) AS per_signal
        WHERE roots <> 1 OR sent_roots <> 1
        """,
        source_id,
    )

    chatter_count = _count(
        session,
        """
        SELECT COUNT(*)
        FROM message_classifications AS mc
        JOIN messages AS m ON m.id = mc.message_id
        WHERE m.source_id = :source_id
          AND mc.classification = 'chatter'
          AND mc.decision_status = 'ignored'
        """,
        source_id,
    )
    chatter_leaks = _count(
        session,
        """
        SELECT COUNT(*)
        FROM message_classifications AS mc
        JOIN messages AS m ON m.id = mc.message_id
        WHERE m.source_id = :source_id
          AND mc.classification = 'chatter'
          AND mc.decision_status = 'ignored'
          AND (
              EXISTS (
                  SELECT 1 FROM signals AS sig
                  WHERE sig.source_message_id = m.id
              )
              OR EXISTS (
                  SELECT 1 FROM signal_lifecycle_events AS sle
                  WHERE sle.source_message_id = m.id
              )
          )
        """,
        source_id,
    )

    edited_message_count = _count(
        session,
        """
        SELECT COUNT(DISTINCT m.id)
        FROM messages AS m
        JOIN message_revisions AS mr ON mr.message_id = m.id
        WHERE m.source_id = :source_id
          AND mr.revision_index > 0
        """,
        source_id,
    )
    edit_link_problems = _count(
        session,
        """
        SELECT COUNT(*)
        FROM (
            SELECT m.id, COUNT(sig.id) AS signal_count
            FROM messages AS m
            JOIN message_revisions AS mr ON mr.message_id = m.id AND mr.revision_index > 0
            LEFT JOIN signals AS sig
              ON sig.source_id = m.source_id
             AND sig.provider_message_id = m.telegram_message_id
            WHERE m.source_id = :source_id
            GROUP BY m.id
            HAVING COUNT(sig.id) > 1
        ) AS duplicate_edit_signal
        """,
        source_id,
    )

    followup_event_count = _count(
        session,
        """
        SELECT COUNT(*)
        FROM signal_lifecycle_events AS sle
        JOIN signals AS sig ON sig.id = sle.signal_id
        JOIN messages AS m ON m.id = sle.source_message_id
        JOIN message_classifications AS mc
          ON mc.message_id = m.id
         AND mc.revision_index = sle.source_revision_index
        WHERE sig.source_id = :source_id
          AND m.source_id = :source_id
          AND mc.classification = 'trade_update'
          AND mc.decision_status = 'classified'
        """,
        source_id,
    )
    followup_link_problems = _count(
        session,
        """
        SELECT COUNT(*)
        FROM signal_lifecycle_events AS sle
        JOIN signals AS sig ON sig.id = sle.signal_id
        JOIN messages AS m ON m.id = sle.source_message_id
        LEFT JOIN telegram_publications AS update_pub
          ON update_pub.lifecycle_event_id = sle.id
        LEFT JOIN telegram_publications AS root_pub
          ON root_pub.signal_id = sig.id
         AND root_pub.publication_kind = 'signal_created'
        WHERE sig.source_id = :source_id
          AND (
              m.source_id <> :source_id
              OR update_pub.id IS NULL
              OR update_pub.status <> 'sent'
              OR root_pub.id IS NULL
              OR root_pub.status <> 'sent'
              OR update_pub.reply_to_telegram_message_id IS DISTINCT FROM root_pub.telegram_message_id
          )
        """,
        source_id,
    )

    classified_count = _count(
        session,
        """
        SELECT COUNT(*)
        FROM message_classifications AS mc
        JOIN messages AS m ON m.id = mc.message_id
        WHERE m.source_id = :source_id
        """,
        source_id,
    )
    missing_classification_audits = _count(
        session,
        """
        SELECT COUNT(*)
        FROM message_classifications AS mc
        JOIN messages AS m ON m.id = mc.message_id
        WHERE m.source_id = :source_id
          AND NOT EXISTS (
              SELECT 1
              FROM audit_events AS ae
              WHERE ae.event_type = 'message.classified'
                AND ae.entity_type = 'message'
                AND ae.entity_id = m.id
                AND COALESCE((ae.payload ->> 'revision_index')::integer, 0) = mc.revision_index
          )
        """,
        source_id,
    )
    missing_signal_audits = _count(
        session,
        """
        SELECT COUNT(*)
        FROM signals AS sig
        WHERE sig.source_id = :source_id
          AND NOT EXISTS (
              SELECT 1 FROM audit_events AS ae
              WHERE ae.event_type = 'signal.created'
                AND ae.entity_type = 'signal'
                AND ae.entity_id = sig.id
          )
        """,
        source_id,
    )
    missing_lifecycle_audits = _count(
        session,
        """
        SELECT COUNT(*)
        FROM signal_lifecycle_events AS sle
        JOIN signals AS sig ON sig.id = sle.signal_id
        WHERE sig.source_id = :source_id
          AND NOT EXISTS (
              SELECT 1 FROM audit_events AS ae
              WHERE ae.event_type = 'signal.lifecycle_event_created'
                AND ae.entity_type = 'signal'
                AND ae.entity_id = sig.id
                AND ae.payload ->> 'lifecycle_event_id' = sle.id::text
          )
        """,
        source_id,
    )
    missing_publication_audits = _count(
        session,
        """
        SELECT COUNT(*)
        FROM telegram_publications AS pub
        JOIN signals AS sig ON sig.id = pub.signal_id
        WHERE sig.source_id = :source_id
          AND pub.status = 'sent'
          AND NOT EXISTS (
              SELECT 1 FROM audit_events AS ae
              WHERE ae.event_type = 'telegram.publication_sent'
                AND ae.entity_type = 'signal'
                AND ae.entity_id = sig.id
                AND ae.payload ->> 'publication_id' = pub.id::text
          )
        """,
        source_id,
    )
    audit_problem_count = (
        missing_classification_audits
        + missing_signal_audits
        + missing_lifecycle_audits
        + missing_publication_audits
    )

    checks = (
        _check(
            key="trade_publish_once",
            evidence_count=signal_count,
            problem_count=root_publication_problems,
            expected="At least one canonical Signal; exactly one sent root publication per Signal.",
            pass_detail="Canonical trade Signals each have one sent Telegram root publication.",
            pending_detail="No canonical Signal exists yet for this gate source.",
            fail_detail="At least one canonical Signal has a missing, duplicate, or unsent root publication.",
        ),
        _check(
            key="chatter_never_publishes",
            evidence_count=chatter_count,
            problem_count=chatter_leaks,
            expected="At least one ignored chatter message and zero Signal/lifecycle leakage.",
            pass_detail="Ignored chatter exists and produced no canonical Signal or lifecycle event.",
            pending_detail="No ignored chatter sample exists yet for this gate source.",
            fail_detail="Ignored chatter leaked into canonical Signal/lifecycle state.",
        ),
        _check(
            key="edits_link_correctly",
            evidence_count=edited_message_count,
            problem_count=edit_link_problems,
            expected="At least one edited provider message and no duplicate canonical Signal identity.",
            pass_detail="Edited provider messages retain one logical canonical Signal identity at most.",
            pending_detail="No edited provider message exists yet for this gate source.",
            fail_detail="An edited provider message created duplicate canonical Signal identity.",
        ),
        _check(
            key="followups_link_correctly",
            evidence_count=followup_event_count,
            problem_count=followup_link_problems,
            expected="At least one valid follow-up; every lifecycle event sent once under the correct root thread.",
            pass_detail="Stored follow-up lifecycle events publish beneath their correct root Signal post.",
            pending_detail="No valid linked lifecycle follow-up exists yet for this gate source.",
            fail_detail="A lifecycle follow-up is missing its sent publication or is attached to the wrong root.",
        ),
        _check(
            key="all_events_auditable",
            evidence_count=classified_count,
            problem_count=audit_problem_count,
            expected="Classification, Signal creation, lifecycle creation, and sent publications all have audit evidence.",
            pass_detail="Every checked Telegram-stage state transition has matching audit evidence.",
            pending_detail="No classified Telegram messages exist yet for this gate source.",
            fail_detail=(
                "Missing audit evidence: "
                f"classification={missing_classification_audits}, "
                f"signal={missing_signal_audits}, "
                f"lifecycle={missing_lifecycle_audits}, "
                f"publication={missing_publication_audits}."
            ),
        ),
    )

    statuses = {check.status for check in checks}
    overall = "failed" if "failed" in statuses else "pending" if "pending" in statuses else "passed"
    return TelegramE2EGateReport(
        source_id=source["id"],
        source_alias=str(source["source_alias"]),
        source_status=str(source["status"]),
        status=overall,
        generated_at=datetime.now(timezone.utc),
        checks=checks,
    )
