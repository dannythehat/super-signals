"""Versioned provider communication/trading footprint for Provider Lab research.

The stored footprint is semantic-stable: it contains no wall-clock refresh timestamp, so
an unchanged refresh cannot manufacture a new PIT profile version. Historical price
geometry is research-only and is deliberately excluded from ``interpretation_context``.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any, Iterable
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

FOOTPRINT_VERSION = "provider-footprint-v1"
_LIMIT = 500
_NUMBER = re.compile(r"(?<![A-Za-z])[-+]?\d+(?:[.,]\d+)?")
_URL = re.compile(r"https?://\S+|t\.me/\S+", re.I)
_TOKEN = re.compile(r"[A-Za-z][A-Za-z'-]{1,24}")
_EMOJI = re.compile(r"[^\x00-\x7F]")
_STOP = {
    "the","and","for","with","this","that","from","your","you","our","are","was",
    "have","has","will","now","here","just","all","not","gold","xau","xauusd","buy",
    "sell","tp","sl","entry","trade","signal",
}


def _rate(n: int, d: int) -> float:
    return round(n / d, 4) if d else 0.0


def _med(values: Iterable[float]) -> float | None:
    vals = [float(v) for v in values]
    return round(float(median(vals)), 4) if vals else None


def _dec(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value)) if value is not None and not isinstance(value, bool) else None
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed is not None and parsed.is_finite() else None


def _session(value: Any) -> str:
    if not isinstance(value, datetime):
        return "unknown"
    return "asia" if value.hour < 7 else "london" if value.hour < 13 else "new_york" if value.hour < 21 else "late"


def _dominant(counts: Counter[str], threshold: float = 0.55) -> str:
    total = sum(counts.values())
    if not total:
        return "unknown"
    key, count = counts.most_common(1)[0]
    return key if count / total >= threshold else "mixed"


def _bucket(value: float, low: float, high: float) -> str:
    return "frequent" if value >= high else "occasional" if value >= low else "rare"


def _tokens(texts: Iterable[str], limit: int = 16) -> list[str]:
    counts: Counter[str] = Counter()
    for raw in texts:
        clean = _NUMBER.sub(" ", _URL.sub(" ", raw or ""))
        for token in _TOKEN.findall(clean.lower()):
            if token not in _STOP:
                counts[token] += 1
    return [token for token, _ in counts.most_common(limit)]


def _format(texts: list[str]) -> str:
    vals = [v for v in texts if v]
    if not vals:
        return "unknown"
    if sum("\n" in v for v in vals) / len(vals) >= 0.55:
        return "structured_multiline"
    if sum(len(v) <= 80 and "\n" not in v for v in vals) / len(vals) >= 0.55:
        return "compact_single_line"
    return "mixed"


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ProviderFootprintService:
    """Persist one source-specific, research-only footprint into versioned profile JSON."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def refresh(self, source_id: UUID) -> dict[str, Any]:
        with self._session_factory() as session:
            source = session.execute(text("SELECT status FROM sources WHERE id=:id"), {"id": source_id}).mappings().first()
            if source is None:
                return {}
            messages = [dict(r) for r in session.execute(text("""
                WITH recent AS (
                  SELECT id,posted_at,deleted_at,raw_text,raw_payload
                  FROM messages WHERE source_id=:source_id
                  ORDER BY posted_at DESC,id DESC LIMIT :lim
                )
                SELECT r.*,
                       COALESCE(x.revision_count,0) AS revision_count,
                       x.first_edit_at
                FROM recent r
                LEFT JOIN LATERAL (
                  SELECT COUNT(*) AS revision_count,MIN(edited_at) AS first_edit_at
                  FROM message_revisions WHERE message_id=r.id
                ) x ON TRUE
                ORDER BY r.posted_at DESC,r.id DESC
            """), {"source_id": source_id, "lim": _LIMIT}).mappings()]
            revisions = [dict(r) for r in session.execute(text("""
                SELECT mr.raw_text,mr.edited_at
                FROM message_revisions mr JOIN messages m ON m.id=mr.message_id
                WHERE m.source_id=:source_id
                ORDER BY mr.edited_at DESC,mr.id DESC LIMIT :lim
            """), {"source_id": source_id, "lim": _LIMIT}).mappings()]
            signals = [dict(r) for r in session.execute(text("""
                SELECT id,side,order_type,entry_low,entry_high,stop_loss,take_profits,
                       has_open_runner,source_revision_index,source_posted_at
                FROM signals WHERE source_id=:source_id AND parser_status='accepted'
                ORDER BY source_posted_at DESC NULLS LAST,created_at DESC,id DESC LIMIT :lim
            """), {"source_id": source_id, "lim": 300}).mappings()]
            events = [dict(r) for r in session.execute(text("""
                SELECT e.event_type,e.occurred_at,s.id AS signal_id,s.source_posted_at
                FROM signal_lifecycle_events e JOIN signals s ON s.id=e.signal_id
                WHERE s.source_id=:source_id AND e.origin='provider_update'
                ORDER BY e.occurred_at DESC,e.created_at DESC LIMIT :lim
            """), {"source_id": source_id, "lim": _LIMIT}).mappings()]
            payload = self.build(source_id, str(source["status"]), messages, revisions, signals, events)
            session.execute(text("""
                INSERT INTO provider_research_profiles(source_id,profile_metadata,updated_at)
                VALUES (:source_id,CAST(:metadata AS jsonb),now())
                ON CONFLICT (source_id) DO UPDATE SET
                  profile_metadata=COALESCE(provider_research_profiles.profile_metadata,'{}'::jsonb)
                                   || CAST(:metadata AS jsonb),
                  updated_at=now()
            """), {"source_id": source_id, "metadata": json.dumps({"footprint_v1": payload}, sort_keys=True)})
            session.commit()
            return payload

    @classmethod
    def build(
        cls,
        source_id: UUID,
        provider_status: str,
        messages: list[dict[str, Any]],
        revisions: list[dict[str, Any]],
        signals: list[dict[str, Any]],
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        texts = [str(r.get("raw_text") or "") for r in messages]
        all_texts = texts + [str(r.get("raw_text") or "") for r in revisions]
        edited = [r for r in messages if int(r.get("revision_count") or 0) > 0]
        deleted = [r for r in messages if r.get("deleted_at") is not None]
        replies = [r for r in messages if isinstance(r.get("raw_payload"), dict) and r["raw_payload"].get("reply_to_message_id") is not None]
        edit_delays = []
        sessions: Counter[str] = Counter()
        active_days = set()
        evidence_times: list[datetime] = []
        for row in messages:
            posted, first_edit = row.get("posted_at"), row.get("first_edit_at")
            if isinstance(posted, datetime):
                evidence_times.append(posted)
                active_days.add(posted.date())
                sessions[_session(posted)] += 1
                if isinstance(first_edit, datetime) and first_edit >= posted:
                    edit_delays.append((first_edit - posted).total_seconds() / 60)
            deleted_at = row.get("deleted_at")
            if isinstance(deleted_at, datetime):
                evidence_times.append(deleted_at)
        evidence_times.extend(r["edited_at"] for r in revisions if isinstance(r.get("edited_at"), datetime))

        n_msg = len(messages)
        edit_rate, delete_rate, reply_rate = _rate(len(edited), n_msg), _rate(len(deleted), n_msg), _rate(len(replies), n_msg)
        vocab = _tokens(all_texts)
        msg_format = _format(texts)
        emoji_rate = _rate(sum(bool(_EMOJI.search(v)) for v in texts), n_msg)

        sides: Counter[str] = Counter()
        orders: Counter[str] = Counter()
        entries: Counter[str] = Counter()
        widths: list[float] = []
        stops: list[float] = []
        tp_counts: list[float] = []
        runners = revisions_used = 0
        for row in signals:
            sides[str(row.get("side") or "unknown").upper()] += 1
            orders[str(row.get("order_type") or "unknown").lower()] += 1
            low, high, stop = _dec(row.get("entry_low")), _dec(row.get("entry_high")), _dec(row.get("stop_loss"))
            if low is not None and high is not None:
                width = abs(high - low)
                widths.append(float(width))
                entries["exact" if width == 0 else "zone"] += 1
                if stop is not None:
                    stops.append(float(abs((low + high) / Decimal("2") - stop)))
            targets = row.get("take_profits")
            if isinstance(targets, (list, tuple)):
                tp_counts.append(float(len(targets)))
            runners += int(bool(row.get("has_open_runner")))
            revisions_used += int(int(row.get("source_revision_index") or 0) > 0)
            if isinstance(row.get("source_posted_at"), datetime):
                evidence_times.append(row["source_posted_at"])

        event_mix: Counter[str] = Counter(str(r.get("event_type") or "unknown") for r in events)
        first_event: dict[str, tuple[datetime, datetime]] = {}
        for row in events:
            sid, occurred, posted = str(row.get("signal_id") or ""), row.get("occurred_at"), row.get("source_posted_at")
            if isinstance(occurred, datetime):
                evidence_times.append(occurred)
            if sid and isinstance(occurred, datetime) and isinstance(posted, datetime) and occurred >= posted:
                current = first_event.get(sid)
                if current is None or occurred < current[1]:
                    first_event[sid] = (posted, occurred)
        management_delays = [(e - p).total_seconds() / 60 for p, e in first_event.values()]

        n_signal = len(signals)
        event_rate = round(len(events) / n_signal, 4) if n_signal else 0.0
        management_style = "active_management" if event_rate >= 0.75 else "moderate_management" if event_rate >= 0.25 else "minimal_management"
        revision_signal_rate = _rate(revisions_used, n_signal)
        sequence = "edit_completed_setups" if revision_signal_rate >= 0.25 else "edit_and_reply_sequence" if edit_rate >= 0.20 and reply_rate >= 0.20 else "reply_chain_management" if reply_rate >= 0.35 else "edit_heavy_sequence" if edit_rate >= 0.20 else "single_post_or_mixed"
        drift = cls._drift(signals[:30], signals[30:60], messages[:60], messages[60:120])

        signature_inputs = {
            "tokens": vocab[:12], "format": msg_format, "session": _dominant(sessions),
            "entry": _dominant(entries), "order": _dominant(orders), "side": _dominant(sides),
            "edit": _bucket(edit_rate, .05, .20), "delete": _bucket(delete_rate, .02, .10),
            "reply": _bucket(reply_rate, .10, .35), "management": management_style,
        }
        interpretation = {
            "profile_version": FOOTPRINT_VERSION,
            "message_sequence": sequence,
            "edit_behaviour": signature_inputs["edit"],
            "deletion_behaviour": signature_inputs["delete"],
            "reply_behaviour": signature_inputs["reply"],
            "dominant_session_utc": signature_inputs["session"],
            "message_format": msg_format,
            "emoji_style": _bucket(emoji_rate, .10, .35),
            "entry_style": signature_inputs["entry"],
            "order_style": signature_inputs["order"],
            "direction_style": signature_inputs["side"],
            "runner_usage": _bucket(_rate(runners, n_signal), .10, .40),
            "management_style": management_style,
            "provider_vocabulary": vocab[:12],
            "drift_status": drift["status"],
            "safety_note": "Behavioural context only; no historical price level is execution evidence.",
        }
        return {
            "profile_version": FOOTPRINT_VERSION,
            "source_id": str(source_id),
            "provider_status": provider_status,
            "evidence_as_of_utc": max(evidence_times).isoformat() if evidence_times else None,
            "research_only": True,
            "live_money_execution_allowed": False,
            "message_behaviour": {
                "observed_messages": n_msg, "revision_rows": len(revisions), "edited_messages": len(edited),
                "deleted_messages": len(deleted), "reply_messages": len(replies), "edit_rate": edit_rate,
                "delete_rate": delete_rate, "reply_rate": reply_rate,
                "median_first_edit_delay_minutes": _med(edit_delays),
                "messages_per_active_day": round(n_msg / max(1, len(active_days)), 3) if n_msg else 0.0,
            },
            "language_fingerprint": {"message_format": msg_format, "top_provider_tokens": vocab, "emoji_rate": emoji_rate},
            "timing_fingerprint": {"active_days": len(active_days), "dominant_session_utc": _dominant(sessions), "session_mix": dict(sorted(sessions.items()))},
            "trade_geometry": {
                "accepted_signals": n_signal, "side_mix": dict(sorted(sides.items())), "order_type_mix": dict(sorted(orders.items())),
                "entry_style_mix": dict(sorted(entries.items())), "median_entry_zone_width": _med(widths),
                "median_stop_distance": _med(stops), "median_tp_count": _med(tp_counts),
                "runner_rate": _rate(runners, n_signal), "signal_from_revision_rate": revision_signal_rate,
                "research_only_numeric_geometry": True,
            },
            "management_fingerprint": {
                "provider_update_events": len(events), "managed_signal_count": len(first_event), "events_per_signal": event_rate,
                "event_mix": dict(sorted(event_mix.items())), "median_first_management_delay_minutes": _med(management_delays),
                "management_style": management_style,
            },
            "identity_fingerprint": {"behaviour_signature_sha256": _digest(signature_inputs), "signature_inputs": signature_inputs, "cross_provider_match_authority": False},
            "drift_fingerprint": drift,
            "interpretation_context": interpretation,
        }

    @classmethod
    def _drift(cls, recent: list[dict[str, Any]], prior: list[dict[str, Any]], recent_msg: list[dict[str, Any]], prior_msg: list[dict[str, Any]]) -> dict[str, Any]:
        if len(recent) < 15 or len(prior) < 15:
            return {"status": "insufficient_forward_samples", "recent_signal_n": len(recent), "prior_signal_n": len(prior), "candidate_reasons": [], "auto_apply": False}
        def rates(rows: list[dict[str, Any]]) -> dict[str, float]:
            n = len(rows)
            buy = sum(str(r.get("side") or "").upper() == "BUY" for r in rows)
            runner = sum(bool(r.get("has_open_runner")) for r in rows)
            zone = sum((_dec(r.get("entry_low")) is not None and _dec(r.get("entry_high")) is not None and _dec(r.get("entry_low")) != _dec(r.get("entry_high"))) for r in rows)
            revision = sum(int(r.get("source_revision_index") or 0) > 0 for r in rows)
            return {"buy_rate": _rate(buy,n), "runner_rate": _rate(runner,n), "zone_rate": _rate(zone,n), "revision_rate": _rate(revision,n)}
        rr, pr = rates(recent), rates(prior)
        reasons = [k for k in rr if abs(rr[k] - pr[k]) >= .35]
        rt, pt = set(_tokens((str(r.get("raw_text") or "") for r in recent_msg),20)), set(_tokens((str(r.get("raw_text") or "") for r in prior_msg),20))
        overlap = round(len(rt & pt) / len(rt | pt), 4) if rt and pt else None
        if len(recent_msg) >= 30 and len(prior_msg) >= 30 and overlap is not None and overlap < .20:
            reasons.append("provider_vocabulary_shift")
        return {"status": "candidate_drift" if reasons else "stable_within_current_gates", "recent_signal_n": len(recent), "prior_signal_n": len(prior), "recent_rates": rr, "prior_rates": pr, "vocabulary_jaccard": overlap, "candidate_reasons": reasons, "auto_apply": False}


__all__ = ["FOOTPRINT_VERSION", "ProviderFootprintService"]
