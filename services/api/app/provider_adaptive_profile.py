"""Adaptive, database-backed provider language and performance profiles.

The profile is learned from each Telegram source's own durable message/decision history.
It stores only non-numeric communication patterns for semantic interpretation. Performance
traits are persisted separately inside the same JSON profile for research/optimisation and
are never supplied as execution evidence to the message interpreter.
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
_DECISION_LIMIT = 240
_SIGNAL_LIMIT = 160
_PERFORMANCE_LIMIT = 240

_NUMBER = re.compile(r"(?<![A-Za-z])[-+]?\d+(?:[.,]\d+)?(?:\s*[-–—/]\s*\d+(?:[.,]\d+)?)?")
_URL = re.compile(r"https?://\S+|t\.me/\S+", re.IGNORECASE)
_SPACE = re.compile(r"\s+")
_GOLD = re.compile(r"\b(?:gold|xau\s*/?\s*usd|xauusd|xau)\b", re.IGNORECASE)
_PENDING = re.compile(r"\b(?:buy|sell)\s+(?:limit|stop)\b|\bpending\b", re.IGNORECASE)
_ZONE = re.compile(r"\b(?:entry|buy|sell)?\s*[:@-]?\s*\d+(?:\.\d+)?\s*[-–—/]\s*\d+(?:\.\d+)?\b", re.IGNORECASE)
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


def _masked(text_value: str, *, limit: int = 260) -> str:
    value = _URL.sub("<URL>", text_value or "")
    value = _NUMBER.sub("<N>", value)
    value = _SPACE.sub(" ", value).strip()
    return value[:limit]


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _minutes(start: Any, end: Any) -> float | None:
    if not isinstance(start, datetime) or not isinstance(end, datetime) or end < start:
        return None
    return round((end - start).total_seconds() / 60.0, 2)


def _session_bucket(value: Any) -> str:
    if not isinstance(value, datetime):
        return "unknown"
    hour = value.hour
    if 0 <= hour < 7:
        return "asia"
    if 7 <= hour < 13:
        return "london"
    if 13 <= hour < 21:
        return "new_york"
    return "late"


def _dominant_bucket(counts: Counter[str], *, minimum_share: float = 0.60, empty: str = "unknown") -> str:
    total = sum(counts.values())
    if total <= 0:
        return empty
    value, count = counts.most_common(1)[0]
    return value if (count / total) >= minimum_share else "mixed"


@dataclass(frozen=True, slots=True)
class AdaptiveProviderProfile:
    source_id: UUID
    payload: dict[str, Any]

    @property
    def language_context(self) -> dict[str, Any]:
        return dict(self.payload.get("language", {}))


class AdaptiveProviderProfileService:
    """Learn, persist and cache source-specific language/performance buckets."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        cache_seconds: int = _CACHE_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._cache_seconds = max(30, int(cache_seconds))
        self._cache: dict[UUID, tuple[float, AdaptiveProviderProfile]] = {}
        self._lock = threading.Lock()

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

    def invalidate(self, source_id: UUID) -> None:
        with self._lock:
            self._cache.pop(source_id, None)

    def _build(self, source_id: UUID) -> AdaptiveProviderProfile:
        with self._session_factory() as session:
            source = session.execute(
                text(
                    """
                    SELECT COALESCE(NULLIF(chat_title,''),NULLIF(source_alias,''),'') AS title,
                           status
                    FROM sources WHERE id=:source_id LIMIT 1
                    """
                ),
                {"source_id": source_id},
            ).mappings().first()
            decisions = self._decision_rows(session, source_id)
            signals = self._signal_rows(session, source_id)
            broker_outcomes = self._broker_outcomes(session, source_id)
            shadow_outcomes = self._shadow_outcomes(session, source_id)
            payload = self._profile_payload(
                source_id=source_id,
                source=source,
                decisions=decisions,
                signals=signals,
                broker_outcomes=broker_outcomes,
                shadow_outcomes=shadow_outcomes,
            )
            self._persist(session, source_id, payload)
            session.commit()
        return AdaptiveProviderProfile(source_id=source_id, payload=payload)

    @staticmethod
    def _decision_rows(session: Session, source_id: UUID) -> list[dict[str, Any]]:
        rows = session.execute(
            text(
                """
                SELECT d.revision_index,d.decision,d.action,d.reason,d.extracted,d.created_at,
                       m.telegram_message_id,m.posted_at,
                       CASE WHEN d.revision_index=0 THEN m.raw_text ELSE mr.raw_text END AS raw_text
                FROM ai_message_decisions d
                JOIN messages m ON m.id=d.message_id
                LEFT JOIN message_revisions mr
                  ON mr.message_id=m.id AND mr.revision_index=d.revision_index
                WHERE m.source_id=:source_id AND m.deleted_at IS NULL
                ORDER BY d.created_at DESC
                LIMIT :limit
                """
            ),
            {"source_id": source_id, "limit": _DECISION_LIMIT},
        ).mappings().all()
        return [dict(row) for row in rows]

    @staticmethod
    def _signal_rows(session: Session, source_id: UUID) -> list[dict[str, Any]]:
        rows = session.execute(
            text(
                """
                SELECT symbol,side,order_type,entry_low,entry_high,take_profits,has_open_runner,
                       source_revision_index,source_posted_at,original_text
                FROM signals
                WHERE source_id=:source_id AND parser_status='accepted'
                ORDER BY source_posted_at DESC NULLS LAST,created_at DESC
                LIMIT :limit
                """
            ),
            {"source_id": source_id, "limit": _SIGNAL_LIMIT},
        ).mappings().all()
        return [dict(row) for row in rows]

    @staticmethod
    def _broker_outcomes(session: Session, source_id: UUID) -> list[dict[str, Any]]:
        rows = session.execute(
            text(
                """
                SELECT side,status,opened_at,closed_at,cash_pnl,return_percent,close_reason
                FROM performance_trade_outcomes
                WHERE source_id=:source_id AND closed_at IS NOT NULL
                  AND status IN ('won','lost','breakeven','closed_unknown')
                ORDER BY closed_at DESC
                LIMIT :limit
                """
            ),
            {"source_id": source_id, "limit": _PERFORMANCE_LIMIT},
        ).mappings().all()
        return [dict(row) for row in rows]

    @staticmethod
    def _shadow_outcomes(session: Session, source_id: UUID) -> list[dict[str, Any]]:
        rows = session.execute(
            text(
                """
                SELECT side,opened_at,closed_at,benchmark_pnl_usd,close_reason
                FROM shadow_trades
                WHERE source_id=:source_id AND status='closed' AND score_eligible
                  AND closed_at IS NOT NULL
                ORDER BY closed_at DESC
                LIMIT :limit
                """
            ),
            {"source_id": source_id, "limit": _PERFORMANCE_LIMIT},
        ).mappings().all()
        return [dict(row) for row in rows]

    @classmethod
    def _profile_payload(
        cls,
        *,
        source_id: UUID,
        source: Any,
        decisions: list[dict[str, Any]],
        signals: list[dict[str, Any]],
        broker_outcomes: list[dict[str, Any]],
        shadow_outcomes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        title = str((source or {}).get("title") or "")
        status = str((source or {}).get("status") or "unknown")
        decision_counts = Counter(str(row.get("decision") or "unknown") for row in decisions)
        executable = [row for row in decisions if row.get("decision") == "new_trade" and row.get("action") == "execute"]
        skipped_trades = [row for row in decisions if row.get("decision") == "new_trade" and row.get("action") != "execute"]
        updates = [row for row in decisions if row.get("decision") == "trade_update"]
        preparations = [row for row in decisions if row.get("decision") == "preparation"]

        entry_counts: Counter[str] = Counter()
        order_counts: Counter[str] = Counter()
        symbol_counts: Counter[str] = Counter()
        runner_count = edited_signal_count = layer_count = 0
        texts: list[str] = []
        for row in signals:
            low = _decimal(row.get("entry_low"))
            high = _decimal(row.get("entry_high"))
            if low is not None and high is not None:
                entry_counts["exact" if low == high else "zone"] += 1
            order_counts[str(row.get("order_type") or "unknown").lower()] += 1
            symbol_counts[str(row.get("symbol") or "unknown").upper()] += 1
            runner_count += int(bool(row.get("has_open_runner")))
            edited_signal_count += int(int(row.get("source_revision_index") or 0) > 0)
            raw_text = str(row.get("original_text") or "")
            layer_count += int(_LAYER.search(raw_text) is not None)
            if raw_text:
                texts.append(raw_text)
        texts.extend(str(row.get("raw_text") or "") for row in decisions if str(row.get("raw_text") or "").strip())
        corpus = "\n".join(texts)

        signal_dates = {row.get("source_posted_at").date() for row in signals if isinstance(row.get("source_posted_at"), datetime)}
        signals_per_day = round(len(signals) / max(1, len(signal_dates)), 3) if signals else 0.0
        if signals_per_day >= 5:
            cadence_bucket = "scalper"
        elif signals_per_day >= 1.5:
            cadence_bucket = "intraday"
        elif 0 < signals_per_day < 0.75:
            cadence_bucket = "swing_or_sparse"
        elif signals_per_day > 0:
            cadence_bucket = "mixed"
        else:
            cadence_bucket = "unknown"

        gold_signals = symbol_counts.get("XAUUSD", 0)
        gold_share = _ratio(gold_signals, sum(symbol_counts.values()))
        instrument_bucket = "xauusd_dedicated" if _GOLD.search(title) or (gold_signals >= 5 and gold_share >= 0.8) else "multi_instrument"
        revision_executes = sum(int(int(row.get("revision_index") or 0) > 0) for row in executable)
        if executable and _ratio(revision_executes, len(executable)) >= 0.25:
            sequence_bucket = "edit_completed_setup"
        elif preparations and executable:
            sequence_bucket = "precursor_then_setup"
        else:
            sequence_bucket = "single_post_or_mixed"
        management_intensity = _ratio(len(updates), max(1, len(executable)))
        management_bucket = "active_management" if management_intensity >= 0.75 else ("moderate_management" if management_intensity >= 0.25 else "minimal_management")

        examples: dict[str, list[str]] = {"new_trade": [], "trade_update": [], "preparation": []}
        for row in decisions:
            decision = str(row.get("decision") or "")
            if decision not in examples or len(examples[decision]) >= 3:
                continue
            value = _masked(str(row.get("raw_text") or ""))
            if value and value not in examples[decision]:
                examples[decision].append(value)

        language = {
            "profile_version": _PROFILE_VERSION,
            "provider_status": status,
            "instrument_bucket": instrument_bucket,
            "cadence_bucket": cadence_bucket,
            "entry_bucket": _dominant_bucket(entry_counts),
            "order_bucket": _dominant_bucket(order_counts),
            "sequence_bucket": sequence_bucket,
            "management_bucket": management_bucket,
            "signals_per_active_day": signals_per_day,
            "accepted_signal_count": len(signals),
            "semantic_new_trade_count": decision_counts.get("new_trade", 0),
            "semantic_execute_count": len(executable),
            "semantic_skipped_trade_count": len(skipped_trades),
            "semantic_trade_update_count": len(updates),
            "execute_rate": _ratio(len(executable), decision_counts.get("new_trade", 0)),
            "traits": {
                "uses_exact_entries": entry_counts.get("exact", 0) > 0,
                "uses_entry_zones": entry_counts.get("zone", 0) > 0 or _ZONE.search(corpus) is not None,
                "uses_pending_orders": order_counts.get("pending", 0) > 0 or _PENDING.search(corpus) is not None,
                "uses_layered_entries": layer_count > 0 or _LAYER.search(corpus) is not None,
                "uses_runner_language": runner_count > 0 or _RUNNER.search(corpus) is not None,
                "uses_breakeven_language": _BREAKEVEN.search(corpus) is not None,
                "uses_partial_language": _PARTIAL.search(corpus) is not None,
                "uses_reentry_language": _REENTRY.search(corpus) is not None,
                "uses_tp_hit_language": _TP_HIT.search(corpus) is not None,
                "uses_sl_hit_language": _SL_HIT.search(corpus) is not None,
                "uses_prepare_language": _PREPARE.search(corpus) is not None,
                "uses_same_message_edits": edited_signal_count > 0 or revision_executes > 0,
            },
            "grammar_examples_masked": examples,
        }
        return {
            "profile_version": _PROFILE_VERSION,
            "source_id": str(source_id),
            "generated_at_epoch": int(time.time()),
            "language": language,
            "performance": cls._performance_profile(broker_outcomes, shadow_outcomes),
        }

    @staticmethod
    def _performance_profile(broker_outcomes: list[dict[str, Any]], shadow_outcomes: list[dict[str, Any]]) -> dict[str, Any]:
        if broker_outcomes:
            rows = broker_outcomes
            source = "broker"
            def pnl(row: dict[str, Any]) -> Decimal:
                return _decimal(row.get("cash_pnl")) or Decimal("0")
            def won(row: dict[str, Any]) -> bool:
                return str(row.get("status") or "") == "won" or pnl(row) > 0
            def lost(row: dict[str, Any]) -> bool:
                return str(row.get("status") or "") == "lost" or pnl(row) < 0
        else:
            rows = shadow_outcomes
            source = "shadow"
            def pnl(row: dict[str, Any]) -> Decimal:
                return _decimal(row.get("benchmark_pnl_usd")) or Decimal("0")
            def won(row: dict[str, Any]) -> bool:
                return pnl(row) > 0
            def lost(row: dict[str, Any]) -> bool:
                return pnl(row) < 0

        wins = [row for row in rows if won(row)]
        losses = [row for row in rows if lost(row)]
        win_minutes = [value for row in wins if (value := _minutes(row.get("opened_at"), row.get("closed_at"))) is not None]
        loss_minutes = [value for row in losses if (value := _minutes(row.get("opened_at"), row.get("closed_at"))) is not None]
        side: dict[str, dict[str, Any]] = {}
        sessions: dict[str, dict[str, Any]] = {}
        for row in rows:
            side_name = str(row.get("side") or "unknown").upper()
            side.setdefault(side_name, {"trades": 0, "wins": 0, "losses": 0})
            side[side_name]["trades"] += 1
            side[side_name]["wins"] += int(won(row))
            side[side_name]["losses"] += int(lost(row))
            bucket = _session_bucket(row.get("opened_at"))
            sessions.setdefault(bucket, {"trades": 0, "wins": 0, "losses": 0})
            sessions[bucket]["trades"] += 1
            sessions[bucket]["wins"] += int(won(row))
            sessions[bucket]["losses"] += int(lost(row))
        for group in (side, sessions):
            for item in group.values():
                item["win_rate_percent"] = round(100 * item["wins"] / item["trades"], 2) if item["trades"] else 0.0

        median_win = round(float(median(win_minutes)), 2) if win_minutes else None
        median_loss = round(float(median(loss_minutes)), 2) if loss_minutes else None
        enough = len(rows) >= 20 and len(wins) >= 5 and len(losses) >= 5
        stale_candidate = bool(enough and median_win and median_loss and median_loss >= median_win * 1.6)
        stale_watch_minutes = None
        if stale_candidate:
            ordered = sorted(win_minutes)
            index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * 0.75))))
            stale_watch_minutes = max(5, int(round(ordered[index])))
        return {
            "evidence_source": source if rows else "none",
            "closed_outcomes": len(rows),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_percent": round(100 * len(wins) / len(rows), 2) if rows else 0.0,
            "median_win_minutes": median_win,
            "median_loss_minutes": median_loss,
            "side_buckets": side,
            "session_buckets_utc": sessions,
            "stale_trade_pattern": {
                "status": "candidate" if stale_candidate else "insufficient_or_not_supported",
                "watch_after_minutes": stale_watch_minutes,
                "minimum_required_closed": 20,
                "minimum_required_wins": 5,
                "minimum_required_losses": 5,
                "auto_apply": False,
            },
        }

    @staticmethod
    def _persist(session: Session, source_id: UUID, payload: dict[str, Any]) -> None:
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


__all__ = ["AdaptiveProviderProfile", "AdaptiveProviderProfileService"]
