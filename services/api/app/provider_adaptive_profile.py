"""Adaptive provider grammar and performance research profiles.

Each monitored Telegram source is learned from its own durable history. Historical numeric
trade values are masked before language examples are supplied to semantic interpretation.
Performance observations are stored for provider optimisation research only and never
become execution evidence for a new signal.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

_PROFILE_VERSION = "adaptive-provider-v1"
_CACHE_SECONDS = 300
_HISTORY_LIMIT = 240

_NUMBER = re.compile(r"(?<![A-Za-z])[-+]?\d+(?:[.,]\d+)?(?:\s*[-–—/]\s*\d+(?:[.,]\d+)?)?")
_URL = re.compile(r"https?://\S+|t\.me/\S+", re.IGNORECASE)
_SPACE = re.compile(r"\s+")
_GOLD = re.compile(r"\b(?:gold|xau\s*/?\s*usd|xauusd|xau)\b", re.IGNORECASE)
_ZONE = re.compile(r"\b(?:entry|buy|sell)?\s*[:@-]?\s*\d+(?:\.\d+)?\s*[-–—/]\s*\d+(?:\.\d+)?\b", re.IGNORECASE)
_PENDING = re.compile(r"\b(?:buy|sell)\s+(?:limit|stop)\b|\bpending\b", re.IGNORECASE)
_LAYER = re.compile(r"\b(?:second|2nd)\s+entry\b|\blayer(?:s|ed|ing)?\b|\bre-?entr(?:y|ies)\b", re.IGNORECASE)
_RUNNER = re.compile(r"\brunner\b|\bleave\s+(?:it\s+)?open\b|\btp\s*(?:\d+\s*)?open\b", re.IGNORECASE)
_BREAKEVEN = re.compile(r"\bbreak\s*even\b|\bbreakeven\b|\brisk\s*free\b|\bmove\s+(?:sl\s+)?to\s+be\b|\bbe\s*(?:now|here)?\b", re.IGNORECASE)
_PARTIAL = re.compile(r"\bclose\s+(?:half|partial)\b|\bpartial(?:s|ly)?\b", re.IGNORECASE)
_REENTRY = re.compile(r"\bre-?entr(?:y|ies)\b|\benter\s+again\b", re.IGNORECASE)
_TP_HIT = re.compile(r"\btp\s*\d*\s*(?:hit|done|reached)\b", re.IGNORECASE)
_SL_HIT = re.compile(r"\bsl\s*(?:hit|done|reached)\b|\bstop\s*loss\s*(?:hit|done|reached)\b", re.IGNORECASE)
_PREPARE = re.compile(r"\b(?:prepare|get\s+ready|watch|wait\s+for|setup\s+coming)\b", re.IGNORECASE)


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def mask_language_example(value: str, *, limit: int = 260) -> str:
    """Keep provider wording while removing reusable prices and external URLs."""
    masked = _URL.sub("<URL>", value or "")
    masked = _NUMBER.sub("<N>", masked)
    return _SPACE.sub(" ", masked).strip()[:limit]


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _minutes(start: Any, end: Any) -> float | None:
    if not isinstance(start, datetime) or not isinstance(end, datetime) or end < start:
        return None
    return round((end - start).total_seconds() / 60.0, 2)


def _session(value: Any) -> str:
    if not isinstance(value, datetime):
        return "unknown"
    hour = value.hour
    if hour < 7:
        return "asia"
    if hour < 13:
        return "london"
    if hour < 21:
        return "new_york"
    return "late"


def _dominant(counts: Counter[str], *, threshold: float = 0.60) -> str:
    total = sum(counts.values())
    if not total:
        return "unknown"
    name, count = counts.most_common(1)[0]
    return name if count / total >= threshold else "mixed"


@dataclass(frozen=True, slots=True)
class AdaptiveProviderProfile:
    source_id: UUID
    payload: dict[str, Any]

    @property
    def language_context(self) -> dict[str, Any]:
        return dict(self.payload.get("language", {}))


class AdaptiveProviderProfileService:
    """Build, persist and cache one provider-specific interpretation profile."""

    def __init__(self, session_factory: sessionmaker[Session], *, cache_seconds: int = _CACHE_SECONDS) -> None:
        self._session_factory = session_factory
        self._cache_seconds = max(30, int(cache_seconds))
        self._cache: dict[UUID, tuple[float, AdaptiveProviderProfile]] = {}
        self._lock = threading.Lock()

    def invalidate(self, source_id: UUID) -> None:
        with self._lock:
            self._cache.pop(source_id, None)

    def get(self, source_id: UUID) -> AdaptiveProviderProfile:
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(source_id)
            if cached is not None and now - cached[0] < self._cache_seconds:
                return cached[1]
        profile = self._build(source_id)
        with self._lock:
            self._cache[source_id] = (now, profile)
        return profile

    def _build(self, source_id: UUID) -> AdaptiveProviderProfile:
        with self._session_factory() as session:
            source = session.execute(
                text("SELECT COALESCE(chat_title,source_alias,'') AS title,status FROM sources WHERE id=:id"),
                {"id": source_id},
            ).mappings().first()
            decisions = [dict(row) for row in session.execute(
                text(
                    """
                    SELECT d.revision_index,d.decision,d.action,d.created_at,m.posted_at,
                           CASE WHEN d.revision_index=0 THEN m.raw_text ELSE mr.raw_text END AS raw_text
                    FROM ai_message_decisions d
                    JOIN messages m ON m.id=d.message_id
                    LEFT JOIN message_revisions mr
                      ON mr.message_id=m.id AND mr.revision_index=d.revision_index
                    WHERE m.source_id=:source_id AND m.deleted_at IS NULL
                    ORDER BY d.created_at DESC LIMIT :limit
                    """
                ),
                {"source_id": source_id, "limit": _HISTORY_LIMIT},
            ).mappings()]
            signals = [dict(row) for row in session.execute(
                text(
                    """
                    SELECT symbol,side,order_type,entry_low,entry_high,has_open_runner,
                           source_revision_index,source_posted_at,original_text
                    FROM signals
                    WHERE source_id=:source_id AND parser_status='accepted'
                    ORDER BY source_posted_at DESC NULLS LAST,created_at DESC LIMIT :limit
                    """
                ),
                {"source_id": source_id, "limit": _HISTORY_LIMIT},
            ).mappings()]
            broker = self._broker_outcomes(session, source_id)
            shadow = [dict(row) for row in session.execute(
                text(
                    """
                    SELECT side,opened_at,closed_at,benchmark_pnl_usd
                    FROM shadow_trades
                    WHERE source_id=:source_id AND status='closed' AND score_eligible
                      AND closed_at IS NOT NULL
                    ORDER BY closed_at DESC LIMIT :limit
                    """
                ),
                {"source_id": source_id, "limit": _HISTORY_LIMIT},
            ).mappings()]
            payload = self._payload(source_id, source, decisions, signals, broker, shadow)
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
                {"source_id": source_id, "metadata": json.dumps({"adaptive_v1": payload}, sort_keys=True)},
            )
            session.commit()
        return AdaptiveProviderProfile(source_id, payload)

    @staticmethod
    def _broker_outcomes(session: Session, source_id: UUID) -> list[dict[str, Any]]:
        """Collapse TP legs to one completed provider signal before behavioural analysis."""
        rows = session.execute(
            text(
                """
                SELECT signal_id,MAX(side) AS side,
                       CASE WHEN SUM(COALESCE(cash_pnl,0))>0 THEN 'won'
                            WHEN SUM(COALESCE(cash_pnl,0))<0 THEN 'lost' ELSE 'breakeven' END AS status,
                       MIN(opened_at) AS opened_at,MAX(closed_at) AS closed_at,
                       SUM(COALESCE(cash_pnl,0)) AS cash_pnl
                FROM performance_trade_outcomes
                WHERE source_id=:source_id
                GROUP BY signal_id
                HAVING BOOL_AND(closed_at IS NOT NULL AND status IN ('won','lost','breakeven','closed_unknown'))
                ORDER BY MAX(closed_at) DESC LIMIT :limit
                """
            ),
            {"source_id": source_id, "limit": _HISTORY_LIMIT},
        ).mappings().all()
        return [dict(row) for row in rows]

    @classmethod
    def _payload(
        cls,
        source_id: UUID,
        source: Any,
        decisions: list[dict[str, Any]],
        signals: list[dict[str, Any]],
        broker: list[dict[str, Any]],
        shadow: list[dict[str, Any]],
    ) -> dict[str, Any]:
        counts = Counter(str(row.get("decision") or "unknown") for row in decisions)
        executes = [row for row in decisions if row.get("decision") == "new_trade" and row.get("action") == "execute"]
        updates = [row for row in decisions if row.get("decision") == "trade_update"]
        preparations = [row for row in decisions if row.get("decision") == "preparation"]
        entries: Counter[str] = Counter()
        orders: Counter[str] = Counter()
        symbols: Counter[str] = Counter()
        corpus_parts: list[str] = []
        edited = runner = 0
        active_days: set[Any] = set()
        for row in signals:
            low, high = _decimal(row.get("entry_low")), _decimal(row.get("entry_high"))
            if low is not None and high is not None:
                entries["exact" if low == high else "zone"] += 1
            orders[str(row.get("order_type") or "unknown").lower()] += 1
            symbols[str(row.get("symbol") or "unknown").upper()] += 1
            edited += int(int(row.get("source_revision_index") or 0) > 0)
            runner += int(bool(row.get("has_open_runner")))
            posted = row.get("source_posted_at")
            if isinstance(posted, datetime):
                active_days.add(posted.date())
            corpus_parts.append(str(row.get("original_text") or ""))
        corpus_parts.extend(str(row.get("raw_text") or "") for row in decisions)
        corpus = "\n".join(corpus_parts)
        per_day = round(len(signals) / max(1, len(active_days)), 3) if signals else 0.0
        cadence = "scalper" if per_day >= 5 else "intraday" if per_day >= 1.5 else "swing_or_sparse" if 0 < per_day < 0.75 else "mixed" if per_day else "unknown"
        revision_executes = sum(int(int(row.get("revision_index") or 0) > 0) for row in executes)
        sequence = "edit_completed_setup" if executes and _ratio(revision_executes, len(executes)) >= 0.25 else "precursor_then_setup" if preparations and executes else "single_post_or_mixed"
        intensity = _ratio(len(updates), max(1, len(executes)))
        management = "active_management" if intensity >= 0.75 else "moderate_management" if intensity >= 0.25 else "minimal_management"
        examples: dict[str, list[str]] = {"new_trade": [], "trade_update": [], "preparation": []}
        for row in decisions:
            kind = str(row.get("decision") or "")
            if kind not in examples or len(examples[kind]) >= 3:
                continue
            masked = mask_language_example(str(row.get("raw_text") or ""))
            if masked and masked not in examples[kind]:
                examples[kind].append(masked)
        title = str((source or {}).get("title") or "")
        gold_count = symbols.get("XAUUSD", 0)
        language = {
            "profile_version": _PROFILE_VERSION,
            "provider_status": str((source or {}).get("status") or "unknown"),
            "instrument_bucket": "xauusd_dedicated" if _GOLD.search(title) or (gold_count >= 5 and _ratio(gold_count, sum(symbols.values())) >= 0.8) else "multi_instrument",
            "cadence_bucket": cadence,
            "entry_bucket": _dominant(entries),
            "order_bucket": _dominant(orders),
            "sequence_bucket": sequence,
            "management_bucket": management,
            "signals_per_active_day": per_day,
            "accepted_signal_count": len(signals),
            "semantic_new_trade_count": counts.get("new_trade", 0),
            "semantic_execute_count": len(executes),
            "semantic_trade_update_count": len(updates),
            "execute_rate": _ratio(len(executes), counts.get("new_trade", 0)),
            "traits": {
                "uses_exact_entries": entries.get("exact", 0) > 0,
                "uses_entry_zones": entries.get("zone", 0) > 0 or bool(_ZONE.search(corpus)),
                "uses_pending_orders": orders.get("pending", 0) > 0 or bool(_PENDING.search(corpus)),
                "uses_layered_entries": bool(_LAYER.search(corpus)),
                "uses_runner_language": runner > 0 or bool(_RUNNER.search(corpus)),
                "uses_breakeven_language": bool(_BREAKEVEN.search(corpus)),
                "uses_partial_language": bool(_PARTIAL.search(corpus)),
                "uses_reentry_language": bool(_REENTRY.search(corpus)),
                "uses_tp_hit_language": bool(_TP_HIT.search(corpus)),
                "uses_sl_hit_language": bool(_SL_HIT.search(corpus)),
                "uses_prepare_language": bool(_PREPARE.search(corpus)),
                "uses_same_message_edits": edited > 0 or revision_executes > 0,
            },
            "grammar_examples_masked": examples,
        }
        return {
            "profile_version": _PROFILE_VERSION,
            "source_id": str(source_id),
            "generated_at_epoch": int(time.time()),
            "language": language,
            "performance": cls._performance(broker, shadow),
        }

    @staticmethod
    def _performance(broker: list[dict[str, Any]], shadow: list[dict[str, Any]]) -> dict[str, Any]:
        rows = broker or shadow
        evidence = "broker" if broker else "shadow" if shadow else "none"
        def pnl(row: dict[str, Any]) -> Decimal:
            return _decimal(row.get("cash_pnl" if broker else "benchmark_pnl_usd")) or Decimal("0")
        wins = [row for row in rows if pnl(row) > 0]
        losses = [row for row in rows if pnl(row) < 0]
        win_times = [value for row in wins if (value := _minutes(row.get("opened_at"), row.get("closed_at"))) is not None]
        loss_times = [value for row in losses if (value := _minutes(row.get("opened_at"), row.get("closed_at"))) is not None]
        side: dict[str, dict[str, Any]] = {}
        sessions: dict[str, dict[str, Any]] = {}
        for row in rows:
            is_win, is_loss = pnl(row) > 0, pnl(row) < 0
            side_name = str(row.get("side") or "unknown").upper()
            bucket = _session(row.get("opened_at"))
            for group, key in ((side, side_name), (sessions, bucket)):
                item = group.setdefault(key, {"trades": 0, "wins": 0, "losses": 0})
                item["trades"] += 1
                item["wins"] += int(is_win)
                item["losses"] += int(is_loss)
        for group in (side, sessions):
            for item in group.values():
                item["win_rate_percent"] = round(100 * item["wins"] / item["trades"], 2) if item["trades"] else 0.0
        median_win = round(float(median(win_times)), 2) if win_times else None
        median_loss = round(float(median(loss_times)), 2) if loss_times else None
        enough = len(rows) >= 20 and len(wins) >= 5 and len(losses) >= 5
        stale = bool(enough and median_win and median_loss and median_loss >= median_win * 1.6)
        watch_after = None
        if stale:
            ordered = sorted(win_times)
            watch_after = max(5, int(round(ordered[int((len(ordered) - 1) * 0.75)])))
        return {
            "evidence_source": evidence,
            "closed_outcomes": len(rows),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_percent": round(100 * len(wins) / len(rows), 2) if rows else 0.0,
            "median_win_minutes": median_win,
            "median_loss_minutes": median_loss,
            "side_buckets": side,
            "session_buckets_utc": sessions,
            "stale_trade_pattern": {
                "status": "candidate" if stale else "insufficient_or_not_supported",
                "watch_after_minutes": watch_after,
                "minimum_required_closed": 20,
                "minimum_required_wins": 5,
                "minimum_required_losses": 5,
                "auto_apply": False,
            },
        }


__all__ = ["AdaptiveProviderProfile", "AdaptiveProviderProfileService", "mask_language_example"]
