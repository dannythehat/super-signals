"""AIDY Provider Intelligence B-F integration layer.

B joins each provider signal to point-in-time market context already captured by AIDY.
C turns the existing provider footprint/adaptive grammar into one durable fingerprint.
D emits research governance/watch evidence without mutating live provider status.
E persists provider-specific interpretation hints without historical price leakage.
F records combined-book directional conflicts as observation-only research evidence.

This module is deliberately broker-isolated. It never places, sizes, changes or closes a
trade and never grants live-money authority.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

CONTRACT_VERSION = "aidy-provider-intelligence-bf-v1"
BOOK_CONTRACT_VERSION = "aidy-provider-book-conflict-v1"


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _float(value: Any) -> float:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return 0.0
    return float(parsed) if parsed.is_finite() else 0.0


def _label(value: Any, *keys: str, fallback: str = "unknown") -> str:
    if not isinstance(value, dict):
        return fallback
    for key in keys:
        raw = value.get(key)
        if raw not in (None, "", [], {}):
            return str(raw)[:80]
    return fallback


def _metadata(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def build_market_context(rows: list[dict[str, Any]], *, accepted_signals: int) -> dict[str, Any]:
    """Summarise PIT AIDY context without inventing context for unattached signals."""
    buckets: dict[str, dict[str, Any]] = {}
    attached_signals: set[str] = set()
    closed_signals = wins = losses = breakeven = 0
    pnl_total = 0.0

    for row in rows:
        signal_id = str(row.get("signal_id") or "")
        if signal_id:
            attached_signals.add(signal_id)
        session_name = _label(row.get("session_json"), "name", "session", "label")
        regime_name = _label(row.get("regime_json"), "name", "regime", "label", "state")
        key = f"{session_name}|{regime_name}"
        bucket = buckets.setdefault(
            key,
            {
                "session": session_name,
                "regime": regime_name,
                "signals": 0,
                "closed_signals": 0,
                "wins": 0,
                "losses": 0,
                "breakeven": 0,
                "benchmark_pnl_total": 0.0,
            },
        )
        bucket["signals"] += 1
        closed_legs = int(row.get("closed_legs") or 0)
        pnl = _float(row.get("benchmark_pnl_usd"))
        if closed_legs > 0:
            closed_signals += 1
            bucket["closed_signals"] += 1
            pnl_total += pnl
            bucket["benchmark_pnl_total"] = round(bucket["benchmark_pnl_total"] + pnl, 6)
            if pnl > 0:
                wins += 1
                bucket["wins"] += 1
            elif pnl < 0:
                losses += 1
                bucket["losses"] += 1
            else:
                breakeven += 1
                bucket["breakeven"] += 1

    attached_count = len(attached_signals)
    coverage = min(1.0, attached_count / accepted_signals) if accepted_signals else 0.0
    return {
        "accepted_signals": accepted_signals,
        "context_attached_signals": attached_count,
        "context_coverage": round(coverage, 4),
        "closed_context_signals": closed_signals,
        "wins": wins,
        "losses": losses,
        "breakeven": breakeven,
        "benchmark_pnl_total": round(pnl_total, 6),
        "buckets": sorted(buckets.values(), key=lambda item: (item["session"], item["regime"])),
        "point_in_time_only": True,
    }


def build_fingerprint(profile_metadata: dict[str, Any]) -> dict[str, Any]:
    footprint = _metadata(profile_metadata.get("footprint_v1"))
    adaptive = _metadata(profile_metadata.get("adaptive_v1"))
    interpretation = _metadata(footprint.get("interpretation_context"))
    identity = _metadata(footprint.get("identity_fingerprint"))
    adaptive_language = _metadata(adaptive.get("language"))
    return {
        "footprint_version": footprint.get("profile_version"),
        "behaviour_signature_sha256": identity.get("behaviour_signature_sha256"),
        "message_sequence": interpretation.get("message_sequence"),
        "dominant_session_utc": interpretation.get("dominant_session_utc"),
        "entry_style": interpretation.get("entry_style"),
        "order_style": interpretation.get("order_style"),
        "management_style": interpretation.get("management_style"),
        "provider_vocabulary": list(interpretation.get("provider_vocabulary") or [])[:12],
        "drift_status": interpretation.get("drift_status") or _metadata(footprint.get("drift_fingerprint")).get("status"),
        "adaptive_profile_version": adaptive.get("profile_version"),
        "cadence_bucket": adaptive_language.get("cadence_bucket"),
        "sequence_bucket": adaptive_language.get("sequence_bucket"),
        "communication_traits": _metadata(adaptive_language.get("traits")),
        "research_only": True,
    }


def build_governance(
    *,
    source_status: str,
    research_state: str | None,
    observed_messages: int,
    accepted_signals: int,
    closed_shadow_legs: int,
    market_context: dict[str, Any],
    fingerprint: dict[str, Any],
) -> dict[str, Any]:
    coverage = float(market_context.get("context_coverage") or 0.0)
    drift = str(fingerprint.get("drift_status") or "unknown").lower()
    reasons: list[str] = []
    disposition = "learning"

    if accepted_signals >= 20 and coverage < 0.25:
        disposition = "quarantine_candidate"
        reasons.append("low_point_in_time_context_coverage")
    elif "drift" in drift and drift not in {"no_drift", "stable"}:
        disposition = "watch"
        reasons.append("provider_fingerprint_drift")
    elif observed_messages >= 20 and accepted_signals == 0:
        disposition = "watch"
        reasons.append("messages_present_but_no_accepted_signals")
    elif accepted_signals >= 5:
        disposition = "healthy_research"
    else:
        reasons.append("insufficient_forward_signal_sample")

    return {
        "research_disposition": disposition,
        "reasons": reasons,
        "source_status_observed": source_status,
        "research_state_observed": research_state,
        "observed_messages": observed_messages,
        "accepted_signals": accepted_signals,
        "closed_shadow_legs": closed_shadow_legs,
        "automatic_source_status_mutation_allowed": False,
        "source_status_mutation_performed": False,
        "live_money_execution_allowed": False,
    }


def build_adaptation(fingerprint: dict[str, Any], profile_metadata: dict[str, Any]) -> dict[str, Any]:
    adaptive = _metadata(profile_metadata.get("adaptive_v1"))
    language = _metadata(adaptive.get("language"))
    examples = _metadata(language.get("grammar_examples_masked"))
    accepted = int(language.get("accepted_signal_count") or 0)
    confidence = "high" if accepted >= 20 else "medium" if accepted >= 5 else "low"
    return {
        "provider_specific": True,
        "confidence": confidence,
        "expected_message_sequence": fingerprint.get("message_sequence") or fingerprint.get("sequence_bucket"),
        "cadence_bucket": fingerprint.get("cadence_bucket"),
        "entry_style": fingerprint.get("entry_style"),
        "order_style": fingerprint.get("order_style"),
        "management_style": fingerprint.get("management_style"),
        "communication_traits": fingerprint.get("communication_traits") or {},
        "masked_grammar_examples": {
            key: list(value)[:3] for key, value in examples.items() if isinstance(value, list)
        },
        "historical_numeric_levels_allowed": False,
        "current_message_or_direct_reply_evidence_required": True,
        "live_money_execution_allowed": False,
    }


def build_book_conflict(signals: list[dict[str, Any]], *, now: datetime | None = None) -> dict[str, Any]:
    """Research-only provider conflict view. It never changes or nets broker orders."""
    observed_at = (now or datetime.now(UTC)).astimezone(UTC)
    latest_by_source: dict[str, dict[str, Any]] = {}
    for row in signals:
        source_id = str(row.get("source_id") or "")
        side = str(row.get("side") or "").upper()
        if not source_id or side not in {"BUY", "SELL"}:
            continue
        posted_at = row.get("source_posted_at")
        current = latest_by_source.get(source_id)
        if current is None or (
            isinstance(posted_at, datetime)
            and isinstance(current.get("source_posted_at"), datetime)
            and posted_at > current["source_posted_at"]
        ):
            latest_by_source[source_id] = row

    buys = sorted(source for source, row in latest_by_source.items() if str(row.get("side") or "").upper() == "BUY")
    sells = sorted(source for source, row in latest_by_source.items() if str(row.get("side") or "").upper() == "SELL")
    conflicts = [{"buy_source_id": buy, "sell_source_id": sell} for buy in buys for sell in sells]
    if buys and sells:
        bias = "long" if len(buys) > len(sells) else "short" if len(sells) > len(buys) else "mixed"
    elif buys:
        bias = "long"
    elif sells:
        bias = "short"
    else:
        bias = "flat"
    return {
        "observed_at": observed_at.isoformat(),
        "window_minutes": 45,
        "provider_count": len(latest_by_source),
        "signal_count": len(latest_by_source),
        "buy_provider_count": len(buys),
        "sell_provider_count": len(sells),
        "conflict_count": len(conflicts),
        "net_bias": bias,
        "conflicts": conflicts,
        "recommended_action": "observe_only",
        "broker_netting_allowed": False,
        "live_money_execution_allowed": False,
    }


class ProviderIntelligenceBuilder:
    """Persist stable, append-only B-F snapshots for Provider Lab and tomorrow's Data Hub."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def _sources(self, session: Session) -> list[dict[str, Any]]:
        return [dict(row) for row in session.execute(text("""
            SELECT s.id AS source_id,
                   COALESCE(NULLIF(s.chat_title,''),NULLIF(s.source_alias,''),'') AS source_name,
                   s.status AS source_status,
                   prp.research_state,
                   COALESCE(prp.profile_metadata,'{}'::jsonb) AS profile_metadata,
                   (SELECT COUNT(*) FROM messages m WHERE m.source_id=s.id) AS observed_messages,
                   (SELECT COUNT(*) FROM signals sig WHERE sig.source_id=s.id AND sig.parser_status='accepted') AS accepted_signals,
                   (SELECT COUNT(*) FROM shadow_trades st WHERE st.source_id=s.id AND st.status='closed' AND st.score_eligible) AS closed_shadow_legs
            FROM sources s
            LEFT JOIN provider_research_profiles prp ON prp.source_id=s.id
            WHERE s.status IN ('testing','shadow','live')
            ORDER BY s.created_at,s.id
        """)).mappings().all()]

    def _contexts(self, session: Session) -> dict[str, list[dict[str, Any]]]:
        rows = session.execute(text("""
            SELECT a.source_id,a.signal_id,a.signal_posted_at,a.session_json,a.regime_json,a.market_json,
                   COALESCE(SUM(st.benchmark_pnl_usd) FILTER (WHERE st.status='closed' AND st.score_eligible),0) AS benchmark_pnl_usd,
                   COUNT(st.id) FILTER (WHERE st.status='closed' AND st.score_eligible) AS closed_legs
            FROM provider_signal_context_attachments a
            LEFT JOIN shadow_trades st ON st.signal_id=a.signal_id
            GROUP BY a.source_id,a.signal_id,a.signal_posted_at,a.session_json,a.regime_json,a.market_json
            ORDER BY a.signal_posted_at,a.signal_id
        """)).mappings().all()
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[str(row["source_id"])].append(dict(row))
        return grouped

    def _recent_signals(self, session: Session, *, now: datetime) -> list[dict[str, Any]]:
        cutoff = now - timedelta(minutes=45)
        return [dict(row) for row in session.execute(text("""
            SELECT id,source_id,side,source_posted_at
            FROM signals
            WHERE parser_status='accepted'
              AND source_posted_at>=:cutoff
              AND source_posted_at<=:now
            ORDER BY source_posted_at,id
        """), {"cutoff": cutoff, "now": now}).mappings().all()]

    def refresh_once(self) -> tuple[int, int]:
        now = datetime.now(UTC)
        with self._session_factory() as session:
            sources = self._sources(session)
            contexts = self._contexts(session)
            recent_signals = self._recent_signals(session, now=now)
            inserted = 0
            for source in sources:
                source_id = UUID(str(source["source_id"]))
                metadata = _metadata(source.get("profile_metadata"))
                accepted = int(source.get("accepted_signals") or 0)
                market = build_market_context(contexts.get(str(source_id), []), accepted_signals=accepted)
                fingerprint = build_fingerprint(metadata)
                governance = build_governance(
                    source_status=str(source.get("source_status") or "unknown"),
                    research_state=str(source.get("research_state")) if source.get("research_state") is not None else None,
                    observed_messages=int(source.get("observed_messages") or 0),
                    accepted_signals=accepted,
                    closed_shadow_legs=int(source.get("closed_shadow_legs") or 0),
                    market_context=market,
                    fingerprint=fingerprint,
                )
                adaptation = build_adaptation(fingerprint, metadata)
                payload = {
                    "contract_version": CONTRACT_VERSION,
                    "source_id": str(source_id),
                    "source_name": str(source.get("source_name") or ""),
                    "market_context": market,
                    "fingerprint": fingerprint,
                    "governance": governance,
                    "adaptation": adaptation,
                    "research_only": True,
                    "live_money_execution_allowed": False,
                }
                digest = _digest(payload)
                result = session.execute(text("""
                    INSERT INTO provider_intelligence_snapshots(
                        id,source_id,observed_at,evidence_as_of_utc,market_context_json,
                        fingerprint_json,governance_json,adaptation_json,snapshot_payload,
                        snapshot_digest,contract_version,research_only,live_money_execution_allowed
                    ) VALUES (
                        :id,:source_id,:observed_at,:evidence_as_of,
                        CAST(:market AS jsonb),CAST(:fingerprint AS jsonb),CAST(:governance AS jsonb),
                        CAST(:adaptation AS jsonb),CAST(:payload AS jsonb),:digest,:contract,true,false
                    )
                    ON CONFLICT (source_id,snapshot_digest) DO NOTHING
                    RETURNING id
                """), {
                    "id": uuid4(), "source_id": source_id, "observed_at": now,
                    "evidence_as_of": now, "market": _canonical(market),
                    "fingerprint": _canonical(fingerprint), "governance": _canonical(governance),
                    "adaptation": _canonical(adaptation), "payload": _canonical(payload),
                    "digest": digest, "contract": CONTRACT_VERSION,
                }).scalar_one_or_none()
                inserted += int(result is not None)

            book = build_book_conflict(recent_signals, now=now)
            stable_book = dict(book)
            stable_book.pop("observed_at", None)
            book_digest = _digest(stable_book)
            book_inserted = session.execute(text("""
                INSERT INTO provider_book_conflict_snapshots(
                    id,observed_at,window_start_utc,window_end_utc,signal_count,provider_count,
                    buy_provider_count,sell_provider_count,conflict_count,net_bias,conflicts_json,
                    snapshot_payload,snapshot_digest,contract_version,research_only,
                    live_money_execution_allowed
                ) VALUES (
                    :id,:observed_at,:window_start,:window_end,:signal_count,:provider_count,
                    :buy_count,:sell_count,:conflict_count,:net_bias,CAST(:conflicts AS jsonb),
                    CAST(:payload AS jsonb),:digest,:contract,true,false
                )
                ON CONFLICT (snapshot_digest) DO NOTHING
                RETURNING id
            """), {
                "id": uuid4(), "observed_at": now, "window_start": now - timedelta(minutes=45),
                "window_end": now, "signal_count": int(book["signal_count"]),
                "provider_count": int(book["provider_count"]),
                "buy_count": int(book["buy_provider_count"]), "sell_count": int(book["sell_provider_count"]),
                "conflict_count": int(book["conflict_count"]), "net_bias": str(book["net_bias"]),
                "conflicts": _canonical(book["conflicts"]), "payload": _canonical(book),
                "digest": book_digest, "contract": BOOK_CONTRACT_VERSION,
            }).scalar_one_or_none()
            session.commit()
        return inserted, int(book_inserted is not None)


__all__ = [
    "BOOK_CONTRACT_VERSION",
    "CONTRACT_VERSION",
    "ProviderIntelligenceBuilder",
    "build_adaptation",
    "build_book_conflict",
    "build_fingerprint",
    "build_governance",
    "build_market_context",
]
