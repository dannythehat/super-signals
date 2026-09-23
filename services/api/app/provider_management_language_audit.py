"""Provider management-language coverage audit.

Research-only historical scan. It never sends broker commands. The live policy reuses
only the lightweight candidate detector so explicit management wording can never be
silently downgraded to chatter.

Numeric values and URLs are masked before examples are persisted in provider metadata.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.critical_entry_policy import augment_management_actions
from app.day27_management_policy import extract_day27_management_actions
from app.provider_adaptive_profile import mask_language_example

AUDIT_VERSION = "provider-management-language-v1"
DEFAULT_HISTORY_LIMIT = 5000

_MANAGEMENT_CANDIDATE = re.compile(
    r"\b(?:"
    r"RISK\s*[- ]?FREE|ZERO\s+RISK|0\s*%\s*RISK|"
    r"BREAK\s*EVEN|BREAKEVEN|SET\s+BE|MOVE\s+(?:THE\s+)?(?:SL|STOP)|"
    r"(?:SL|STOP\s*LOSS)\s+(?:TO|AT|HIT|TOUCHED|TRIGGERED)|"
    r"(?:CLOSE|CLOSED|BOOK|TAKE|BANK|COLLECT|SECURE)\s+"
    r"(?:ALL|HALF|PARTIALS?|MOST|PROFITS?|SOME|FIRST\s+LAYER|ENTR(?:Y|IES))|"
    r"TP\s*\d+\s*(?:HIT|HITS|REACHED|TAPPED)|"
    r"(?:HIT|HITS|REACHED|TAPPED)\s+TP\s*\d+|"
    r"SL\s*(?:HIT|TOUCHED|TRIGGERED)|STOPPED\s+OUT|"
    r"(?:CANCEL|REMOVE)\s+(?:THE\s+)?(?:ORDER|LIMIT|PENDING|TRADE)|"
    r"TRAIL\s+(?:THE\s+)?(?:SL|STOP|ENTRY)"
    r")\b",
    re.IGNORECASE,
)

_FAMILIES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("breakeven", re.compile(
        r"\b(?:RISK\s*[- ]?FREE|ZERO\s+RISK|0\s*%\s*RISK|"
        r"BREAK\s*EVEN|BREAKEVEN|SET\s+BE)\b", re.I
    )),
    ("partial", re.compile(
        r"\b(?:PARTIALS?|CLOSE\s+HALF|BOOK\s+(?:SOME|PARTIAL|MORE|MAXIMUM)|"
        r"COLLECT\s+(?:HALF|PARTIAL|FIRST\s+LAYER))\b", re.I
    )),
    ("close", re.compile(r"\b(?:CLOSE|CLOSED|OUT\s+AT|EXIT)\b", re.I)),
    ("stop_update", re.compile(
        r"\b(?:MOVE|SET|PUT)\s+(?:THE\s+)?(?:SL|STOP)|\bSL\s+(?:TO|AT)\b",
        re.I,
    )),
    ("tp_result", re.compile(
        r"\bTP\s*\d+\s*(?:HIT|HITS|REACHED|TAPPED)|"
        r"\b(?:HIT|HITS|REACHED|TAPPED)\s+TP\s*\d+", re.I
    )),
    ("sl_result", re.compile(
        r"\bSL\s*(?:HIT|TOUCHED|TRIGGERED)|\bSTOPPED\s+OUT\b", re.I
    )),
    ("cancel", re.compile(r"\b(?:CANCEL|REMOVE)\b", re.I)),
    ("trail", re.compile(r"\bTRAIL\b", re.I)),
)


def is_management_language_candidate(raw_text: str) -> bool:
    return _MANAGEMENT_CANDIDATE.search(raw_text or "") is not None


def management_phrase_families(raw_text: str) -> tuple[str, ...]:
    text_value = raw_text or ""
    return tuple(name for name, pattern in _FAMILIES if pattern.search(text_value))


@dataclass(frozen=True, slots=True)
class ProviderManagementLanguageAudit:
    source_id: UUID
    payload: dict[str, Any]


class ProviderManagementLanguageAuditService:
    """Audit deterministic management coverage per provider on the research DB lane."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
    ) -> None:
        self._session_factory = session_factory
        self._history_limit = max(100, min(int(history_limit), 5000))

    def refresh_all(self) -> int:
        with self._session_factory() as session:
            source_ids = session.execute(
                text(
                    """
                    SELECT id
                    FROM sources
                    WHERE status <> 'revoked'
                    ORDER BY created_at,id
                    """
                )
            ).scalars().all()
        completed = 0
        for value in source_ids:
            self.refresh_source(UUID(str(value)))
            completed += 1
        return completed

    def refresh_source(self, source_id: UUID) -> ProviderManagementLanguageAudit:
        with self._session_factory() as session:
            source = session.execute(
                text(
                    """
                    SELECT COALESCE(NULLIF(chat_title,''),source_alias,'') AS provider,status
                    FROM sources
                    WHERE id=:source_id
                    """
                ),
                {"source_id": source_id},
            ).mappings().first()
            rows = session.execute(
                text(
                    """
                    SELECT telegram_message_id,posted_at,raw_text
                    FROM messages
                    WHERE source_id=:source_id
                      AND deleted_at IS NULL
                    ORDER BY posted_at DESC,telegram_message_id DESC
                    LIMIT :limit
                    """
                ),
                {"source_id": source_id, "limit": self._history_limit},
            ).mappings().all()

            payload = self._audit_payload(source_id, source, rows)
            session.execute(
                text(
                    """
                    INSERT INTO provider_research_profiles(source_id,profile_metadata,updated_at)
                    VALUES (:source_id,CAST(:metadata AS jsonb),now())
                    ON CONFLICT (source_id) DO UPDATE SET
                      profile_metadata=COALESCE(provider_research_profiles.profile_metadata,'{}'::jsonb)
                                       || CAST(:metadata AS jsonb),
                      updated_at=now()
                    """
                ),
                {
                    "source_id": source_id,
                    "metadata": json.dumps(
                        {"management_language_audit_v1": payload},
                        sort_keys=True,
                    ),
                },
            )
            session.commit()
        return ProviderManagementLanguageAudit(source_id, payload)

    @staticmethod
    def _audit_payload(
        source_id: UUID,
        source: Any,
        rows: list[Any],
    ) -> dict[str, Any]:
        candidate_count = 0
        covered_count = 0
        families: Counter[str] = Counter()
        covered_examples: list[str] = []
        unmapped_examples: list[str] = []

        for row in rows:
            raw = str(row["raw_text"] or "")
            if not is_management_language_candidate(raw):
                continue
            candidate_count += 1
            for family in management_phrase_families(raw):
                families[family] += 1

            policy = extract_day27_management_actions(raw)
            actions = augment_management_actions(raw, policy.actions)
            masked = mask_language_example(raw, limit=220)
            if actions:
                covered_count += 1
                if masked and masked not in covered_examples and len(covered_examples) < 12:
                    covered_examples.append(masked)
            elif masked and masked not in unmapped_examples and len(unmapped_examples) < 20:
                unmapped_examples.append(masked)

        unmapped_count = candidate_count - covered_count
        coverage_pct = (
            round((100.0 * covered_count / candidate_count), 2)
            if candidate_count
            else 100.0
        )
        return {
            "version": AUDIT_VERSION,
            "source_id": str(source_id),
            "provider": str((source or {}).get("provider") or ""),
            "source_status": str((source or {}).get("status") or "unknown"),
            "messages_scanned": len(rows),
            "management_candidates": candidate_count,
            "covered_candidates": covered_count,
            "unmapped_candidates": unmapped_count,
            "coverage_pct": coverage_pct,
            "phrase_families": dict(sorted(families.items())),
            "covered_examples_masked": covered_examples,
            "unmapped_examples_masked": unmapped_examples,
            "execution_authority": False,
            "safety_note": (
                "Research-only audit. It may identify provider language gaps but cannot "
                "mutate trades, sources.status, provider authority, or broker state."
            ),
        }


__all__ = [
    "AUDIT_VERSION",
    "ProviderManagementLanguageAudit",
    "ProviderManagementLanguageAuditService",
    "is_management_language_candidate",
    "management_phrase_families",
]
