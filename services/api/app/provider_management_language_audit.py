"""Provider management-language and trading-playbook audit.

Research-only historical scan. It never sends broker commands.

The live policy reuses only the lightweight candidate detector so explicit management
wording can never be silently downgraded to chatter. The historical audit persists two
reserved namespaces:
- management_language_audit_v1: deterministic wording coverage and unmapped examples.
- provider_playbook_v1: stable behavioural summary of how the provider usually trades.

Historical prices/outcomes are intentionally excluded from the playbook so the profile
cannot become hindsight execution evidence. Numeric values and URLs are masked before
message examples are persisted.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.critical_entry_policy import augment_management_actions
from app.day27_management_policy import extract_day27_management_actions
from app.provider_adaptive_profile import mask_language_example

AUDIT_VERSION = "provider-management-language-v1"
PLAYBOOK_VERSION = "provider-playbook-v1"
DEFAULT_HISTORY_LIMIT = 5000
DEFAULT_SIGNAL_LIMIT = 2500

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
    (
        "breakeven",
        re.compile(
            r"\b(?:RISK\s*[- ]?FREE|ZERO\s+RISK|0\s*%\s*RISK|"
            r"BREAK\s*EVEN|BREAKEVEN|SET\s+BE)\b",
            re.I,
        ),
    ),
    (
        "partial",
        re.compile(
            r"\b(?:PARTIALS?|CLOSE\s+HALF|BOOK\s+(?:SOME|PARTIAL|MORE|MAXIMUM)|"
            r"COLLECT\s+(?:HALF|PARTIAL|FIRST\s+LAYER))\b",
            re.I,
        ),
    ),
    ("close", re.compile(r"\b(?:CLOSE|CLOSED|OUT\s+AT|EXIT)\b", re.I)),
    (
        "stop_update",
        re.compile(
            r"\b(?:MOVE|SET|PUT)\s+(?:THE\s+)?(?:SL|STOP)|\bSL\s+(?:TO|AT)\b",
            re.I,
        ),
    ),
    (
        "tp_result",
        re.compile(
            r"\bTP\s*\d+\s*(?:HIT|HITS|REACHED|TAPPED)|"
            r"\b(?:HIT|HITS|REACHED|TAPPED)\s+TP\s*\d+",
            re.I,
        ),
    ),
    (
        "sl_result",
        re.compile(
            r"\bSL\s*(?:HIT|TOUCHED|TRIGGERED)|\bSTOPPED\s+OUT\b",
            re.I,
        ),
    ),
    ("cancel", re.compile(r"\b(?:CANCEL|REMOVE)\b", re.I)),
    ("trail", re.compile(r"\bTRAIL\b", re.I)),
)


def is_management_language_candidate(raw_text: str) -> bool:
    return _MANAGEMENT_CANDIDATE.search(raw_text or "") is not None


def management_phrase_families(raw_text: str) -> tuple[str, ...]:
    text_value = raw_text or ""
    return tuple(name for name, pattern in _FAMILIES if pattern.search(text_value))


def _percentage(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(100.0 * numerator / denominator, 2)


def _mode(counter: Counter[Any], *, default: Any = None) -> Any:
    if not counter:
        return default
    top = max(counter.values())
    winners = sorted(
        (key for key, count in counter.items() if count == top),
        key=lambda value: str(value),
    )
    return winners[0] if winners else default


def _json_tp_count(value: Any) -> int:
    if isinstance(value, (list, tuple)):
        return len(value)
    if isinstance(value, dict):
        return len(value)
    return 0


def _reply_to_message_id(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get("reply_to_message_id")
    if isinstance(value, int):
        return value
    reply = payload.get("reply_to")
    if isinstance(reply, dict):
        nested = reply.get("reply_to_message_id") or reply.get("message_id")
        if isinstance(nested, int):
            return nested
    return None


@dataclass(frozen=True, slots=True)
class ProviderManagementLanguageAudit:
    source_id: UUID
    payload: dict[str, Any]
    playbook: dict[str, Any]


class ProviderManagementLanguageAuditService:
    """Audit every provider's trading dialect and stable trading behaviour."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
        signal_limit: int = DEFAULT_SIGNAL_LIMIT,
    ) -> None:
        self._session_factory = session_factory
        self._history_limit = max(100, min(int(history_limit), 5000))
        self._signal_limit = max(100, min(int(signal_limit), 5000))

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
                    SELECT
                      COALESCE(NULLIF(s.chat_title,''),s.source_alias,'') AS provider,
                      s.status,
                      pr.style,
                      pr.profile_metadata->'adaptive_v1'->'language'->>'cadence_bucket' AS cadence,
                      pr.profile_metadata->'adaptive_v1'->'language'->>'management_bucket'
                        AS management_bucket
                    FROM sources s
                    LEFT JOIN provider_research_profiles pr ON pr.source_id=s.id
                    WHERE s.id=:source_id
                    """
                ),
                {"source_id": source_id},
            ).mappings().first()
            message_rows = session.execute(
                text(
                    """
                    SELECT
                      telegram_message_id,posted_at,edited_at,raw_text,raw_payload
                    FROM messages
                    WHERE source_id=:source_id
                      AND deleted_at IS NULL
                    ORDER BY posted_at DESC,telegram_message_id DESC
                    LIMIT :limit
                    """
                ),
                {"source_id": source_id, "limit": self._history_limit},
            ).mappings().all()
            signal_rows = session.execute(
                text(
                    """
                    SELECT
                      s.order_type,
                      s.side,
                      s.stop_loss,
                      s.take_profits,
                      s.risk_multiplier,
                      s.has_open_runner,
                      s.entry_low,
                      s.entry_high,
                      s.created_at,
                      m.raw_payload,
                      m.edited_at
                    FROM signals s
                    JOIN messages m ON m.id=s.source_message_id
                    WHERE s.source_id=:source_id
                      AND s.parser_status='accepted'
                    ORDER BY s.created_at DESC,s.id DESC
                    LIMIT :limit
                    """
                ),
                {"source_id": source_id, "limit": self._signal_limit},
            ).mappings().all()

            audit_payload = self._audit_payload(source_id, source, message_rows)
            playbook_payload = self._playbook_payload(
                source_id,
                source,
                message_rows,
                signal_rows,
                audit_payload,
            )
            metadata = {
                "management_language_audit_v1": audit_payload,
                "provider_playbook_v1": playbook_payload,
            }
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
                    "metadata": json.dumps(metadata, sort_keys=True),
                },
            )
            session.commit()
        return ProviderManagementLanguageAudit(
            source_id,
            audit_payload,
            playbook_payload,
        )

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
                if masked and masked not in covered_examples and len(covered_examples) < 16:
                    covered_examples.append(masked)
            elif masked and masked not in unmapped_examples and len(unmapped_examples) < 24:
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
            "trading_style": str((source or {}).get("style") or "unknown"),
            "cadence_bucket": str((source or {}).get("cadence") or "unknown"),
            "management_bucket": str(
                (source or {}).get("management_bucket") or "unknown"
            ),
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

    @staticmethod
    def _playbook_payload(
        source_id: UUID,
        source: Any,
        message_rows: list[Any],
        signal_rows: list[Any],
        audit_payload: dict[str, Any],
    ) -> dict[str, Any]:
        order_types: Counter[str] = Counter()
        sides: Counter[str] = Counter()
        tp_counts: Counter[int] = Counter()
        explicit_sl = 0
        runners = 0
        double_size = 0
        entry_zones = 0

        for row in signal_rows:
            order_types[str(row["order_type"] or "unknown").lower()] += 1
            sides[str(row["side"] or "unknown").upper()] += 1
            tp_counts[_json_tp_count(row["take_profits"])] += 1
            explicit_sl += int(row["stop_loss"] is not None)
            runners += int(bool(row["has_open_runner"]))
            try:
                risk_multiplier = Decimal(str(row["risk_multiplier"] or 1))
            except Exception:
                risk_multiplier = Decimal("1")
            double_size += int(risk_multiplier > Decimal("1"))
            low = row["entry_low"]
            high = row["entry_high"]
            entry_zones += int(low is not None and high is not None and low != high)

        management_messages = [
            row
            for row in message_rows
            if is_management_language_candidate(str(row["raw_text"] or ""))
        ]
        reply_linked = sum(
            1
            for row in management_messages
            if _reply_to_message_id(row["raw_payload"]) is not None
        )
        edited_messages = sum(1 for row in message_rows if row["edited_at"] is not None)
        signal_count = len(signal_rows)
        management_count = len(management_messages)

        preferred_order = _mode(order_types, default="unknown")
        usual_tp_count = _mode(tp_counts, default=0)
        playbook_state = (
            "rich"
            if len(message_rows) >= 100 and signal_count >= 20
            else "developing"
            if len(message_rows) >= 20 or signal_count >= 5
            else "sparse"
        )

        return {
            "version": PLAYBOOK_VERSION,
            "source_id": str(source_id),
            "provider": str((source or {}).get("provider") or ""),
            "source_status": str((source or {}).get("status") or "unknown"),
            "knowledge_state": playbook_state,
            "trading_style": str((source or {}).get("style") or "unknown"),
            "cadence_bucket": str((source or {}).get("cadence") or "unknown"),
            "management_bucket": str(
                (source or {}).get("management_bucket") or "unknown"
            ),
            "messages_scanned": len(message_rows),
            "signals_observed": signal_count,
            "preferred_order_type": preferred_order,
            "order_type_counts": dict(sorted(order_types.items())),
            "side_counts": dict(sorted(sides.items())),
            "usual_tp_count": int(usual_tp_count or 0),
            "tp_count_distribution": {
                str(key): value for key, value in sorted(tp_counts.items())
            },
            "explicit_stop_loss_pct": _percentage(explicit_sl, signal_count),
            "open_runner_pct": _percentage(runners, signal_count),
            "risk_multiplier_above_one_pct": _percentage(double_size, signal_count),
            "entry_zone_pct": _percentage(entry_zones, signal_count),
            "management_updates_observed": management_count,
            "reply_linked_management_pct": _percentage(reply_linked, management_count),
            "standalone_management_pct": _percentage(
                management_count - reply_linked,
                management_count,
            ),
            "edited_message_pct": _percentage(edited_messages, len(message_rows)),
            "management_phrase_families": dict(
                audit_payload.get("phrase_families") or {}
            ),
            "management_language_coverage_pct": float(
                audit_payload.get("coverage_pct") or 0.0
            ),
            "unmapped_management_count": int(
                audit_payload.get("unmapped_candidates") or 0
            ),
            "covered_management_examples_masked": list(
                audit_payload.get("covered_examples_masked") or []
            )[:12],
            "unmapped_management_examples_masked": list(
                audit_payload.get("unmapped_examples_masked") or []
            )[:12],
            "execution_authority": False,
            "safety_note": (
                "Behavioural context only. Historical prices and outcomes are excluded. "
                "Current-message/direct-reply evidence remains mandatory for execution."
            ),
        }


__all__ = [
    "AUDIT_VERSION",
    "PLAYBOOK_VERSION",
    "ProviderManagementLanguageAudit",
    "ProviderManagementLanguageAuditService",
    "is_management_language_candidate",
    "management_phrase_families",
]
