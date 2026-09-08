"""Provider Footprint v1: descriptive provider behaviour research.

This module learns how one Telegram source communicates and manages trades from its
own durable evidence.  It is intentionally observational and research-only.  Raw
historical price levels are never exposed through ``interpretation_context`` and no
output from this module has execution authority.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any, Iterable
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

FOOTPRINT_VERSION = "provider-footprint-v1"
_MESSAGE_LIMIT = 500
_REVISION_LIMIT = 500
_SIGNAL_LIMIT = 300
_EVENT_LIMIT = 600

_NUMBER = re.compile(r"(?<![A-Za-z])[-+]?\d+(?:[.,]\d+)?")
_URL = re.compile(r"https?://\S+|t\.me/\S+", re.IGNORECASE)
_TOKEN = re.compile(r"[A-Za-z][A-Za-z'-]{1,24}")
_EMOJI = re.compile(r"[^\x00-\x7F]")

_STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "from", "your", "you", "our",
    "are", "was", "have", "has", "will", "now", "here", "just", "all", "not",
    "gold", "xau", "xauusd", "buy", "sell", "tp", "sl", "entry", "trade", "signal",
}


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _median(values: Iterable[float]) -> float | None:
    items = [float(value) for value in values]
    return round(float(median(items)), 4) if items else None


def _session_bucket(value: Any) -> str:
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


def _dominant(counter: Counter[str], *, threshold: float = 0.55) -> str:
    total = sum(counter.values())
    if not total:
        return "unknown"
    key, count = counter.most_common(1)[0]
    return key if count / total >= threshold else "mixed"


def _behaviour_bucket(rate: float, *, low: float, high: float) -> str:
    if rate >= high:
        return "frequent"
    if rate >= low:
        return "occasional"
    return "rare"


def _safe_text(value: Any) -> str:
    return str(value or "")


def _tokens(texts: Iterable[str], *, limit: int = 16) -> list[str]:
    counts: Counter[str] = Counter()
    for raw in texts:
        cleaned = _NUMBER.sub(" ", _URL.sub(" ", raw or ""))
        for token in _TOKEN.findall(cleaned.lower()):
            if token in _STOPWORDS or len(token) < 2:
                continue
            counts[token] += 1
    return [token for token, _ in counts.most_common(limit)]


def _message_format(texts: Iterable[str]) -> str:
    values = [value for value in texts if value]
    if not values:
        return "unknown"
    multiline = sum("\n" in value for value in values)
    compact = sum(len(value) <= 80 and "\n" not in value for value in values)
    if multiline / len(values) >= 0.55:
        return "structured_multiline"
    if compact / len(values) >= 0.55:
        return "compact_single_line"
    return "mixed"


def _digest(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ProviderFootprintService:
    """Build and persist one provider's versioned descriptive footprint."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def refresh(self, source_id: UUID) -> dict[str, Any]:
        with self._session_factory() as session:
            source = session.execute(
                text("SELECT COALESCE(chat_title,source_alias,'') AS title,status FROM sources WHERE id=:id"),
                {"id": source_id},
            ).mappings().first()
            if source is None:
                return self.empty(source_id)

            messages = self._load_messages(session, source_id)
            revisions = self._load_revisions(session, source_id)
            signals = self._load_signals(session, source_id)
            management = self._load_management(session, source_id)
            payload = self.build(
                source_id=source_id,
                source=dict(source),
                messages=messages,
                revisions=revisions,
                signals=signals,
                management=management,
            )
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
                    "metadata": json.dumps({"footprint_v1": payload}, sort_keys=True),
                },
            )
            session.commit()
            return payload

    @staticmethod
    def _load_messages(session: Session, source_id: UUID) -> list[dict[str, Any]]:
        rows = session.execute(
            text(
                """
                WITH recent AS (
                    SELECT id,posted_at,edited_at,deleted_at,raw_text,raw_payload
                    FROM messages
                    WHERE source_id=:source_id
                    ORDER BY posted_at DESC,id DESC
                    LIMIT :limit
                )
                SELECT r.id,r.posted_at,r.edited_at,r.deleted_at,r.raw_text,r.raw_payload,
                       COALESCE(x.revision_count,0) AS revision_count,
                       x.first_edit_at,x.last_edit_at
                FROM recent r
                LEFT JOIN LATERAL (
                    SELECT COUNT(*) AS revision_count,
                           MIN(mr.edited_at) AS first_edit_at,
                           MAX(mr.edited_at) AS last_edit_at
                    FROM message_revisions mr
                    WHERE mr.message_id=r.id
                ) x ON TRUE
                ORDER BY r.posted_at DESC,r.id DESC
                """
            ),
            {"source_id": source_id, "limit": _MESSAGE_LIMIT},
        ).mappings().all()
        return [dict(row) for row in rows]

    @staticmethod
    def _load_revisions(session: Session, source_id: UUID) -> list[dict[str, Any]]:
        rows = session.execute(
            text(
                """
                SELECT mr.raw_text,mr.edited_at,mr.revision_index,m.posted_at
                FROM message_revisions mr
                JOIN messages m ON m.id=mr.message_id
                WHERE m.source_id=:source_id
                ORDER BY mr.edited_at DESC,mr.id DESC
                LIMIT :limit
                """
            ),
            {"source_id": source_id, "limit": _REVISION_LIMIT},
        ).mappings().all()
        return [dict(row) for row in rows]

    @staticmethod
    def _load_signals(session: Session, source_id: UUID) -> list[dict[str, Any]]:
        rows = session.execute(
            text(
                """
                SELECT id,side,order_type,entry_low,entry_high,stop_loss,take_profits,
                       has_open_runner,source_revision_index,source_posted_at
                FROM signals
                WHERE source_id=:source_id AND parser_status='accepted'
                ORDER BY source_posted_at DESC NULLS LAST,created_at DESC,id DESC
                LIMIT :limit
                """
            ),
            {"source_id": source_id, "limit": _SIGNAL_LIMIT},
        ).mappings().all()
        return [dict(row) for row in rows]

    @staticmethod
    def _load_management(session: Session, source_id: UUID) -> list[dict[str, Any]]:
        rows = session.execute(
            text(
                """
                SELECT e.event_type,e.occurred_at,s.id AS signal_id,s.source_posted_at
                FROM signal_lifecycle_events e
                JOIN signals s ON s.id=e.signal_id
                WHERE s.source_id=:source_id AND e.origin='provider_update'
                ORDER BY e.occurred_at DESC,e.created_at DESC
                LIMIT :limit
                """
            ),
            {"source_id": source_id, "limit": _EVENT_LIMIT},
        ).mappings().all()
        return [dict(row) for row in rows]

    @classmethod
    def empty(cls, source_id: UUID) -> dict[str, Any]:
        return cls.build(
            source_id=source_id,
            source={"title": "", "status": "unknown"},
            messages=[],
            revisions=[],
            signals=[],
            management=[],
        )

    @classmethod
    def build(
        cls,
        *,
        source_id: UUID,
        source: dict[str, Any],
        messages: list[dict[str, Any]],
        revisions: list[dict[str, Any]],
        signals: list[dict[str, Any]],
        management: list[dict[str, Any]],
    ) -> dict[str, Any]:
        original_texts = [_safe_text(row.get("raw_text")) for row in messages]
        revision_texts = [_safe_text(row.get("raw_text")) for row in revisions]
        all_texts = original_texts + revision_texts

        edited = [row for row in messages if int(row.get("revision_count") or 0) > 0]
        deleted = [row for row in messages if row.get("deleted_at") is not None]
        replies = []
        edit_delays: list[float] = []
        revision_counts: list[float] = []
        session_counts: Counter[str] = Counter()
        active_days: set[Any] = set()
        for row in messages:
            payload = row.get("raw_payload") if isinstance(row.get("raw_payload"), dict) else {}
            if payload.get("reply_to_message_id") is not None:
                replies.append(row)
            posted = row.get("posted_at")
            first_edit = row.get("first_edit_at")
            if isinstance(posted, datetime):
                active_days.add(posted.date())
                session_counts[_session_bucket(posted)] += 1
                if isinstance(first_edit, datetime) and first_edit >= posted:
                    edit_delays.append((first_edit - posted).total_seconds() / 60.0)
            count = int(row.get("revision_count") or 0)
            if count > 0:
                revision_counts.append(float(count))

        message_count = len(messages)
        edit_rate = _rate(len(edited), message_count)
        delete_rate = _rate(len(deleted), message_count)
        reply_rate = _rate(len(replies), message_count)
        messages_per_day = round(message_count / max(1, len(active_days)), 3) if message_count else 0.0
        vocabulary = _tokens(all_texts)
        format_bucket = _message_format(original_texts)
        emoji_rate = _rate(sum(bool(_EMOJI.search(value)) for value in original_texts), message_count)
        uppercase_rate = _rate(
            sum(bool(value) and sum(ch.isupper() for ch in value) >= max(3, sum(ch.isalpha() for ch in value) * 0.55) for value in original_texts),
            message_count,
        )

        sides: Counter[str] = Counter()
        orders: Counter[str] = Counter()
        entry_styles: Counter[str] = Counter()
        stop_distances: list[float] = []
        entry_widths: list[float] = []
        tp_counts: list[float] = []
        runner_count = 0
        revision_signal_count = 0
        for row in signals:
            side = str(row.get("side") or "unknown").upper()
            sides[side] += 1
            orders[str(row.get("order_type") or "unknown").lower()] += 1
            low = _decimal(row.get("entry_low"))
            high = _decimal(row.get("entry_high"))
            stop = _decimal(row.get("stop_loss"))
            if low is not None and high is not None:
                width = abs(high - low)
                entry_widths.append(float(width))
                entry_styles["exact" if width == 0 else "zone"] += 1
                midpoint = (low + high) / Decimal("2")
                if stop is not None:
                    stop_distances.append(float(abs(midpoint - stop)))
            targets = row.get("take_profits")
            if isinstance(targets, (list, tuple)):
                tp_counts.append(float(len(targets)))
            runner_count += int(bool(row.get("has_open_runner")))
            revision_signal_count += int(int(row.get("source_revision_index") or 0) > 0)

        event_counts: Counter[str] = Counter(str(row.get("event_type") or "unknown") for row in management)
        managed_signal_ids = {str(row.get("signal_id")) for row in management if row.get("signal_id") is not None}
        management_delays: list[float] = []
        first_by_signal: dict[str, tuple[datetime, datetime]] = {}
        for row in management:
            signal_id = str(row.get("signal_id") or "")
            occurred = row.get("occurred_at")
            posted = row.get("source_posted_at")
            if not signal_id or not isinstance(occurred, datetime) or not isinstance(posted, datetime) or occurred < posted:
                continue
            current = first_by_signal.get(signal_id)
            if current is None or occurred < current[1]:
                first_by_signal[signal_id] = (posted, occurred)
        for posted, occurred in first_by_signal.values():
            management_delays.append((occurred - posted).total_seconds() / 60.0)

        signal_count = len(signals)
        management_intensity = _rate(len(management), max(1, signal_count))
        management_bucket = (
            "active_management" if management_intensity >= 0.75
            else "moderate_management" if management_intensity >= 0.25
            else "minimal_management"
        )

        recent = signals[:30]
        prior = signals[30:60]
        drift = cls._drift(recent, prior, messages[:60], messages[60:120])

        behavioural_signature_payload = {
            "tokens": vocabulary[:12],
            "format": format_bucket,
            "dominant_session": _dominant(session_counts),
            "entry_style": _dominant(entry_styles),
            "order_style": _dominant(orders),
            "side_style": _dominant(sides),
            "edit_bucket": _behaviour_bucket(edit_rate, low=0.05, high=0.20),
            "delete_bucket": _behaviour_bucket(delete_rate, low=0.02, high=0.10),
            "reply_bucket": _behaviour_bucket(reply_rate, low=0.10, high=0.35),
            "management": management_bucket,
        }
        signature = _digest(behavioural_signature_payload)

        interpretation_context = {
            "profile_version": FOOTPRINT_VERSION,
            "message_sequence": cls._sequence_bucket(
                edit_rate=edit_rate,
                reply_rate=reply_rate,
                revision_signal_rate=_rate(revision_signal_count, signal_count),
            ),
            "edit_behaviour": _behaviour_bucket(edit_rate, low=0.05, high=0.20),
            "deletion_behaviour": _behaviour_bucket(delete_rate, low=0.02, high=0.10),
            "reply_behaviour": _behaviour_bucket(reply_rate, low=0.10, high=0.35),
            "dominant_session_utc": _dominant(session_counts),
            "message_format": format_bucket,
            "uppercase_style": _behaviour_bucket(uppercase_rate, low=0.10, high=0.35),
            "emoji_style": _behaviour_bucket(emoji_rate, low=0.10, high=0.35),
            "entry_style": _dominant(entry_styles),
            "order_style": _dominant(orders),
            "direction_style": _dominant(sides),
            "runner_usage": _behaviour_bucket(_rate(runner_count, signal_count), low=0.10, high=0.40),
            "management_style": management_bucket,
            "provider_vocabulary": vocabulary[:12],
            "drift_status": drift["status"],
            "safety_note": "Behavioural context only. No historical price level is execution evidence.",
        }

        return {
            "profile_version": FOOTPRINT_VERSION,
            "source_id": str(source_id),
            "provider_status": str(source.get("status") or "unknown"),
            "generated_at_epoch": int(time.time()),
            "research_only": True,
            "live_money_execution_allowed": False,
            "message_behaviour": {
                "observed_messages": message_count,
                "revision_rows": len(revisions),
                "edited_messages": len(edited),
                "deleted_messages": len(deleted),
                "reply_messages": len(replies),
                "edit_rate": edit_rate,
                "delete_rate": delete_rate,
                "reply_rate": reply_rate,
                "median_first_edit_delay_minutes": _median(edit_delays),
                "median_revisions_when_edited": _median(revision_counts),
                "messages_per_active_day": messages_per_day,
            },
            "language_fingerprint": {
                "message_format": format_bucket,
                "top_provider_tokens": vocabulary,
                "emoji_rate": emoji_rate,
                "uppercase_style_rate": uppercase_rate,
            },
            "timing_fingerprint": {
                "active_days": len(active_days),
                "dominant_session_utc": _dominant(session_counts),
                "session_mix": dict(sorted(session_counts.items())),
            },
            "trade_geometry": {
                "accepted_signals": signal_count,
                "side_mix": dict(sorted(sides.items())),
                "order_type_mix": dict(sorted(orders.items())),
                "entry_style_mix": dict(sorted(entry_styles.items())),
                "median_entry_zone_width": _median(entry_widths),
                "median_stop_distance": _median(stop_distances),
                "median_tp_count": _median(tp_counts),
                "runner_rate": _rate(runner_count, signal_count),
                "signal_from_revision_rate": _rate(revision_signal_count, signal_count),
                "research_only_numeric_geometry": True,
            },
            "management_fingerprint": {
                "provider_update_events": len(management),
                "managed_signal_count": len(managed_signal_ids),
                "events_per_signal": round(len(management) / signal_count, 4) if signal_count else 0.0,
                "event_mix": dict(sorted(event_counts.items())),
                "median_first_management_delay_minutes": _median(management_delays),
                "management_style": management_bucket,
            },
            "identity_fingerprint": {
                "behaviour_signature_sha256": signature,
                "signature_inputs": behavioural_signature_payload,
                "cross_provider_match_authority": False,
                "auto_merge_provider_identity": False,
            },
            "drift_fingerprint": drift,
            "interpretation_context": interpretation_context,
        }

    @staticmethod
    def _sequence_bucket(*, edit_rate: float, reply_rate: float, revision_signal_rate: float) -> str:
        if revision_signal_rate >= 0.25:
            return "edit_completed_setups"
        if edit_rate >= 0.20 and reply_rate >= 0.20:
            return "edit_and_reply_sequence"
        if reply_rate >= 0.35:
            return "reply_chain_management"
        if edit_rate >= 0.20:
            return "edit_heavy_sequence"
        return "single_post_or_mixed"

    @classmethod
    def _drift(
        cls,
        recent_signals: list[dict[str, Any]],
        prior_signals: list[dict[str, Any]],
        recent_messages: list[dict[str, Any]],
        prior_messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if len(recent_signals) < 15 or len(prior_signals) < 15:
            return {
                "status": "insufficient_forward_samples",
                "recent_signal_n": len(recent_signals),
                "prior_signal_n": len(prior_signals),
                "candidate_reasons": [],
                "auto_apply": False,
            }

        def signal_rates(rows: list[dict[str, Any]]) -> dict[str, float]:
            n = len(rows)
            buy = sum(str(row.get("side") or "").upper() == "BUY" for row in rows)
            runner = sum(bool(row.get("has_open_runner")) for row in rows)
            zone = 0
            revision = 0
            for row in rows:
                low, high = _decimal(row.get("entry_low")), _decimal(row.get("entry_high"))
                zone += int(low is not None and high is not None and low != high)
                revision += int(int(row.get("source_revision_index") or 0) > 0)
            return {
                "buy_rate": _rate(buy, n),
                "runner_rate": _rate(runner, n),
                "zone_rate": _rate(zone, n),
                "revision_rate": _rate(revision, n),
            }

        recent_rates = signal_rates(recent_signals)
        prior_rates = signal_rates(prior_signals)
        reasons = [
            key
            for key in sorted(recent_rates)
            if abs(recent_rates[key] - prior_rates[key]) >= 0.35
        ]

        recent_tokens = set(_tokens((_safe_text(row.get("raw_text")) for row in recent_messages), limit=20))
        prior_tokens = set(_tokens((_safe_text(row.get("raw_text")) for row in prior_messages), limit=20))
        token_overlap = None
        if recent_tokens and prior_tokens:
            token_overlap = round(len(recent_tokens & prior_tokens) / len(recent_tokens | prior_tokens), 4)
            if len(recent_messages) >= 30 and len(prior_messages) >= 30 and token_overlap < 0.20:
                reasons.append("provider_vocabulary_shift")

        return {
            "status": "candidate_drift" if reasons else "stable_within_current_gates",
            "recent_signal_n": len(recent_signals),
            "prior_signal_n": len(prior_signals),
            "recent_rates": recent_rates,
            "prior_rates": prior_rates,
            "vocabulary_jaccard": token_overlap,
            "candidate_reasons": reasons,
            "auto_apply": False,
        }


__all__ = ["FOOTPRINT_VERSION", "ProviderFootprintService"]
