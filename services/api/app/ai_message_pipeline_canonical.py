"""Canonical provider-message decision pipeline.

One production pipeline owns semantic interpretation, mechanical V1 gating, source
profiles and edit handling. Deterministic evidence is used first where conclusive;
OpenAI resolves provider language that is not. Source profiles may supply only a known
instrument identity for dedicated Gold providers and may normalise obvious side-word
typos; they never donate price, SL, TP, order type, size or management target.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from app.ai_message_pipeline import AiPipelineResult
from app.ai_message_supervisor import AiMessageDecision
from app.ai_source_aware_pipeline import SourceAwareAiMessagePipeline
from app.provider_language_profiles import execution_profile_id
from app.v1_message_policy import DECLARED_XAUUSD_PROFILE, apply_v1_message_policy

_LITERAL_PENDING = re.compile(
    r"\b(?:BUY|SELL)\s+(?:LIMITS?|STOPS?)(?:\s+ORDER)?\b|\bPENDING\b",
    re.IGNORECASE,
)
_PRESENT_TENSE_NUMERIC_ENTRY = re.compile(
    r"\b(?:I\s*['’]?\s*M|I\s+AM)\s+"
    r"(?:BUYING|SELLING|SELING|SELLIMG)\s+"
    r"(?:(?:NOW|IF\s+WE\s+TAP(?:\s+IT)?)\s+)?"
    r"(?:(?:GOLD|XAUUSD)\s+)?"
    r"\d+(?:\.\d+)?\b",
    re.IGNORECASE | re.MULTILINE,
)
_TGC_SELL_TYPO = re.compile(r"\b(?:SELING|SELLIMG)\b", re.IGNORECASE)
_EDIT_FRESHNESS = timedelta(minutes=5)


def _empty_extracted() -> dict[str, Any]:
    return {
        "symbol": None,
        "side": None,
        "order_type": None,
        "entry_low": None,
        "entry_high": None,
        "stop_loss": None,
        "take_profits": [],
        "double_lot": False,
        "update_type": None,
        "update_target": None,
        "update_value": None,
        "provider_claimed_pips": None,
    }


def _token(value: Decimal) -> str:
    return format(value.normalize(), "f")


def explicit_management_without_ai(raw_text: str) -> AiMessageDecision | None:
    """Return deterministic semantic evidence for explicit broker management."""
    from app.day27_management_policy import extract_day27_management_actions

    policy = extract_day27_management_actions(raw_text or "")
    if not policy.actions:
        return None
    actions = [dict(action) for action in policy.actions]
    first = actions[0]
    extracted = _empty_extracted()
    extracted.update(
        {
            "management_actions": actions,
            "update_type": first.get("type"),
            "update_target": first.get("target"),
            "update_value": first.get("value"),
        }
    )
    return AiMessageDecision(
        decision="trade_update",
        action="apply_update",
        confidence=1.0,
        reason=f"deterministic_no_ai_{policy.reason}",
        extracted=extracted,
        model="canonical-deterministic-v1",
        response_id=None,
        latency_ms=0,
        source="deterministic_no_ai",
        raw_text_sha256=sha256((raw_text or "").encode("utf-8")).hexdigest(),
    )


class CanonicalAiMessagePipeline(SourceAwareAiMessagePipeline):
    """Only production decision transaction for every provider revision."""

    def _source_profile(self, source_id: UUID) -> str | None:
        """Resolve the language profile for a source, or its declared instrument.

        A hand-written profile carries grammar knowledge as well as instrument identity,
        so it keeps priority. A source with no profile but an owner-set
        ``declared_instrument`` resolves to DECLARED_XAUUSD_PROFILE, which the execution
        policy accepts as an instrument identity and nothing else. That is how a group
        connected after the hardcoded list was written can publish "Buy at 4392.63 SL
        4377.63" and be understood, without anyone editing a dictionary in code.
        """
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT COALESCE(NULLIF(chat_title,''),NULLIF(source_alias,'')) AS name,
                           declared_instrument
                    FROM sources WHERE id=:source_id LIMIT 1
                    """
                ),
                {"source_id": source_id},
            ).mappings().one_or_none()
        if row is None:
            return None
        profile = execution_profile_id(str(row["name"]) if row["name"] else None)
        if profile is not None:
            return profile
        if str(row["declared_instrument"] or "").strip().upper() == "XAUUSD":
            return DECLARED_XAUUSD_PROFILE
        return None

    @staticmethod
    def _apply_profile(decision: AiMessageDecision, profile: str | None) -> AiMessageDecision:
        if profile is None or decision.decision != "new_trade":
            return decision
        extracted = dict(decision.extracted or {})
        extracted["source_profile"] = profile
        if not extracted.get("symbol"):
            extracted["symbol"] = "XAUUSD"
        return replace(decision, extracted=extracted)

    @staticmethod
    def _policy_text(raw_text: str, profile: str | None) -> str:
        # The stored/provider text remains untouched. Only an obvious side-word typo on
        # the dedicated TGC source is normalised for mechanical side verification.
        if profile == "tgc_xauusd":
            return _TGC_SELL_TYPO.sub("SELLING", raw_text or "")
        return raw_text or ""

    @staticmethod
    def _literal_order_type_precedence(
        decision: AiMessageDecision,
        raw_text: str,
    ) -> AiMessageDecision:
        if decision.decision != "new_trade":
            return decision
        extracted = dict(decision.extracted or {})
        if (
            str(extracted.get("order_type") or "").strip().lower() == "pending"
            and _LITERAL_PENDING.search(raw_text or "") is None
        ):
            extracted["order_type"] = "market"
            return replace(decision, extracted=extracted)
        return decision

    def _decide(
        self,
        *,
        source_id: UUID,
        telegram_message_id: int,
        revision_index: int,
        raw_text: str,
        source_status: str,
        reply_context: str | None,
        previous_text: str | None,
    ) -> AiMessageDecision:
        management = explicit_management_without_ai(raw_text)
        if management is not None:
            return management

        deterministic = self._deterministic_fallback(
            source_id=source_id,
            telegram_message_id=telegram_message_id,
            revision_index=revision_index,
            raw_text=raw_text,
        )
        profile = self._source_profile(source_id)
        if deterministic.decision == "new_trade" and deterministic.action == "execute":
            return self._literal_order_type_precedence(
                self._apply_profile(
                    replace(
                        deterministic,
                        model="canonical-deterministic-v1",
                        source="deterministic_no_ai",
                        reason="deterministic_known_trade_no_ai",
                    ),
                    profile,
                ),
                raw_text,
            )

        if (
            deterministic.decision == "chatter"
            and deterministic.action == "ignore"
            and _PRESENT_TENSE_NUMERIC_ENTRY.search(raw_text or "") is None
        ):
            return deterministic

        semantic = super()._decide(
            source_id=source_id,
            telegram_message_id=telegram_message_id,
            revision_index=revision_index,
            raw_text=raw_text,
            source_status=source_status,
            reply_context=reply_context,
            previous_text=previous_text,
        )
        semantic = self._apply_profile(semantic, profile)
        return self._literal_order_type_precedence(semantic, raw_text)

    def _process_revision(
        self,
        source_id: UUID,
        telegram_message_id: int,
        *,
        revision_index: int,
    ) -> AiPipelineResult:
        """Persist exactly one semantic/mechanical decision and its canonical effect."""
        with self._session_factory() as session:
            row = self._load_revision(
                session,
                source_id=source_id,
                telegram_message_id=telegram_message_id,
                revision_index=revision_index,
            )
            if row is None:
                return AiPipelineResult(False, None, None, None, None, None, "message_not_actionable")
            existing = self._existing_decision(session, row["message_id"], revision_index)
            if existing is not None:
                return AiPipelineResult(
                    False,
                    existing["decision"],
                    existing["action"],
                    self._signals.signal_id_for_message(row["message_id"]),
                    None,
                    existing["decision_source"],
                    "decision_already_exists",
                )
            reply_context = self._reply_context(session, source_id, row["raw_payload"])
            previous_text = self._previous_text(session, row["message_id"], revision_index)
            existing_signal_id = self._signals.signal_id_for_message(row["message_id"])
            execution_started = (
                self._execution_started(session, existing_signal_id)
                if existing_signal_id is not None
                else False
            )

        raw_text = str(row["raw_text"] or "")
        if not raw_text.strip():
            decision = self._non_actionable_without_ai(raw_text, reason="empty_message")
            self._store_decision(row["message_id"], revision_index, decision)
            return AiPipelineResult(True, decision.decision, decision.action, existing_signal_id, None, decision.source, decision.reason)
        if revision_index > 0 and previous_text is not None and raw_text == previous_text:
            decision = self._non_actionable_without_ai(raw_text, reason="identical_revision")
            self._store_decision(row["message_id"], revision_index, decision)
            return AiPipelineResult(True, decision.decision, decision.action, existing_signal_id, None, decision.source, decision.reason)

        if revision_index > 0 and existing_signal_id is not None and execution_started:
            revised = self._post_execution_revision(
                source_id=source_id,
                telegram_message_id=telegram_message_id,
                revision_index=revision_index,
                row=row,
                signal_id=existing_signal_id,
                raw_text=raw_text,
                previous_text=previous_text,
                reply_context=reply_context,
            )
            if revised is not None:
                return revised

        decision = self._decide(
            source_id=source_id,
            telegram_message_id=telegram_message_id,
            revision_index=revision_index,
            raw_text=raw_text,
            source_status=str(row["source_status"]),
            reply_context=reply_context,
            previous_text=previous_text,
        )
        profile = self._source_profile(source_id)
        policy_text = self._policy_text(raw_text, profile)
        decision = apply_v1_message_policy(
            decision,
            raw_text=policy_text,
            is_edit=revision_index > 0,
            original_has_signal=(existing_signal_id is not None) if revision_index > 0 else None,
            previous_text=(self._policy_text(previous_text, profile) if previous_text is not None else None),
        )

        if revision_index > 0 and existing_signal_id is not None and execution_started:
            observation_fingerprint = sha256(
                (
                    f"post-execution-edit:{existing_signal_id}:{row['message_id']}:"
                    f"{revision_index}:{raw_text}"
                ).encode("utf-8")
            ).hexdigest()
            self._signals.record_observation(
                signal_id=existing_signal_id,
                message_id=row["message_id"],
                revision_index=revision_index,
                disposition="post_execution_edit",
                fingerprint=observation_fingerprint,
            )
            self._store_decision(row["message_id"], revision_index, decision)
            return AiPipelineResult(
                True,
                decision.decision,
                decision.action,
                existing_signal_id,
                None,
                decision.source,
                "post_execution_edit_evidence_only",
            )

        self._store_decision(row["message_id"], revision_index, decision)
        signal_id: UUID | None = existing_signal_id
        lifecycle_event_id: UUID | None = None
        dispatch_reason = decision.reason

        if revision_index > 0 and existing_signal_id is not None:
            if decision.decision == "new_trade":
                revision_result = self._signals.revise(
                    message_id=row["message_id"],
                    extracted=decision.extracted,
                    revision_index=revision_index,
                    allow_revision=(decision.action == "execute"),
                    reason=decision.reason,
                )
                signal_id = revision_result.signal_id
                dispatch_reason = revision_result.reason
        elif decision.decision == "new_trade" and decision.action == "execute":
            signal_result = self._signals.process(
                message_id=row["message_id"],
                extracted=decision.extracted,
                revision_index=revision_index,
            )
            signal_id = signal_result.signal_id
            dispatch_reason = signal_result.reason
        elif decision.decision == "trade_update" and decision.action == "apply_update":
            lifecycle_result = self._lifecycle.process(
                message_id=row["message_id"],
                extracted=decision.extracted,
                revision_index=revision_index,
            )
            signal_id = lifecycle_result.signal_id
            lifecycle_event_id = lifecycle_result.event_id
            dispatch_reason = lifecycle_result.reason

        return AiPipelineResult(
            True,
            decision.decision,
            decision.action,
            signal_id,
            lifecycle_event_id,
            decision.source,
            dispatch_reason,
        )

    def _post_execution_revision(
        self,
        *,
        source_id: UUID,
        telegram_message_id: int,
        revision_index: int,
        row: Any,
        signal_id: UUID,
        raw_text: str,
        previous_text: str | None,
        reply_context: str | None,
    ) -> AiPipelineResult | None:
        """Convert a fresh completed same-message edit into protection management only."""
        with self._session_factory() as session:
            candidate = session.execute(
                text(
                    """
                    SELECT mr.edited_at,
                           EXISTS(
                               SELECT 1 FROM positions p
                               WHERE p.signal_id=:signal_id
                                 AND p.broker_position_id IS NOT NULL
                                 AND p.status='open'
                           ) AS has_open_broker_position
                    FROM message_revisions mr
                    WHERE mr.message_id=:message_id
                      AND mr.revision_index=:revision_index
                    LIMIT 1
                    """
                ),
                {
                    "signal_id": signal_id,
                    "message_id": row["message_id"],
                    "revision_index": revision_index,
                },
            ).mappings().first()
        if candidate is None or not bool(candidate["has_open_broker_position"]):
            return None
        edited_at = candidate["edited_at"]
        if not isinstance(edited_at, datetime):
            return None
        edited_at = edited_at.replace(tzinfo=UTC) if edited_at.tzinfo is None else edited_at.astimezone(UTC)
        if datetime.now(UTC) - edited_at > _EDIT_FRESHNESS:
            return None

        semantic = self._decide(
            source_id=source_id,
            telegram_message_id=telegram_message_id,
            revision_index=revision_index,
            raw_text=raw_text,
            source_status=str(row["source_status"]),
            reply_context=reply_context,
            previous_text=previous_text,
        )
        profile = self._source_profile(source_id)
        candidate_trade = replace(semantic, decision="new_trade", action="execute")
        gated = apply_v1_message_policy(
            candidate_trade,
            raw_text=self._policy_text(raw_text, profile),
            is_edit=True,
            original_has_signal=True,
            previous_text=(self._policy_text(previous_text, profile) if previous_text is not None else None),
        )
        if gated.decision != "new_trade" or gated.action != "execute":
            self._store_decision(row["message_id"], revision_index, gated)
            return AiPipelineResult(True, gated.decision, gated.action, signal_id, None, gated.source, gated.reason)

        try:
            trade = self._signals._parse_extracted(gated.extracted)
        except ValueError:
            self._store_decision(row["message_id"], revision_index, gated)
            return AiPipelineResult(True, "non_actionable", "skip", signal_id, None, gated.source, "post_execution_revision_incomplete")

        if trade.entry_low is None or trade.entry_high is None:
            # A post-entry completion is meaningful only when the provider has now
            # supplied a complete literal trade definition; bare NOW fallback remains
            # execution-derived and cannot rewrite provider truth here.
            return None

        with self._session_factory() as session:
            existing_tp_indices = {
                int(value)
                for value in session.execute(
                    text(
                        """
                        SELECT DISTINCT tp_index FROM positions
                        WHERE signal_id=:signal_id
                          AND broker_position_id IS NOT NULL
                          AND status='open'
                        """
                    ),
                    {"signal_id": signal_id},
                ).scalars().all()
            }
            actions: list[dict[str, str | None]] = [
                {"type": "edit_stop_loss", "target": "all", "value": _token(trade.stop_loss)}
            ]
            represented_tps = 0
            for index, target in enumerate(trade.take_profits, start=1):
                if index not in existing_tp_indices:
                    continue
                represented_tps += 1
                actions.append({"type": "edit_take_profit", "target": f"tp{index}", "value": _token(target)})

            fingerprint_payload = {
                "signal_id": str(signal_id),
                "message_id": str(row["message_id"]),
                "revision_index": revision_index,
                "symbol": trade.symbol,
                "side": trade.side,
                "order_type": trade.order_type,
                "entry_low": _token(trade.entry_low),
                "entry_high": _token(trade.entry_high),
                "stop_loss": _token(trade.stop_loss),
                "take_profits": [_token(value) for value in trade.take_profits],
                "has_open_runner": trade.has_open_runner,
            }
            fingerprint = sha256(json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            session.execute(
                text(
                    """
                    UPDATE signals
                    SET source_revision_index=:revision_index,
                        source_posted_at=:edited_at,
                        signal_fingerprint=:fingerprint,
                        symbol=:symbol,side=:side,order_type=:order_type,
                        entry_low=:entry_low,entry_high=:entry_high,
                        stop_loss=:stop_loss,take_profits=CAST(:take_profits AS jsonb),
                        has_open_runner=:has_open_runner,parser_status='accepted',
                        skip_reason=NULL,risk_multiplier=:risk_multiplier,
                        original_text=:original_text,updated_at=now()
                    WHERE id=:signal_id
                    """
                ),
                {
                    "signal_id": signal_id,
                    "revision_index": revision_index,
                    "edited_at": edited_at,
                    "fingerprint": fingerprint,
                    "symbol": trade.symbol,
                    "side": trade.side,
                    "order_type": trade.order_type,
                    "entry_low": trade.entry_low,
                    "entry_high": trade.entry_high,
                    "stop_loss": trade.stop_loss,
                    "take_profits": json.dumps([_token(value) for value in trade.take_profits]),
                    "has_open_runner": trade.has_open_runner,
                    "risk_multiplier": trade.size_multiplier,
                    "original_text": raw_text,
                },
            )
            session.execute(
                text(
                    """
                    INSERT INTO signal_observations(
                        signal_id,message_id,revision_index,disposition,observed_fingerprint
                    ) VALUES (:signal_id,:message_id,:revision_index,'canonical',:fingerprint)
                    ON CONFLICT (message_id,revision_index) DO NOTHING
                    """
                ),
                {"signal_id": signal_id, "message_id": row["message_id"], "revision_index": revision_index, "fingerprint": fingerprint},
            )
            event_key = f"post-execution-edit:{row['message_id']}:{revision_index}"
            aggregate = {
                "revised_instruction": {
                    **dict(gated.extracted),
                    "management_actions": actions,
                    "update_type": actions[0]["type"],
                    "update_target": actions[0]["target"],
                    "update_value": actions[0]["value"],
                },
                "same_message_revision": True,
                "entry_reexecution_allowed": False,
                "provider_tp_count": len(trade.take_profits),
                "represented_tp_count": represented_tps,
                "unrepresented_tp_count": max(0, len(trade.take_profits) - represented_tps),
            }
            event_id = uuid4()
            session.execute(
                text(
                    """
                    INSERT INTO signal_lifecycle_events(
                        id,signal_id,source_message_id,source_revision_index,event_type,
                        event_key,origin,rendered_text,pips,aggregate_result,occurred_at
                    ) VALUES (
                        :id,:signal_id,:message_id,:revision_index,'signal_revision',
                        :event_key,'provider_update',
                        'Provider completed trade details in the original Telegram message.',
                        NULL,CAST(:aggregate AS jsonb),:occurred_at
                    ) ON CONFLICT (event_key) DO NOTHING
                    """
                ),
                {
                    "id": event_id,
                    "signal_id": signal_id,
                    "message_id": row["message_id"],
                    "revision_index": revision_index,
                    "event_key": event_key,
                    "aggregate": json.dumps(aggregate, separators=(",", ":"), default=str),
                    "occurred_at": edited_at,
                },
            )
            persisted_event_id = session.execute(
                text("SELECT id FROM signal_lifecycle_events WHERE event_key=:event_key"),
                {"event_key": event_key},
            ).scalar_one()
            session.execute(
                text(
                    """
                    INSERT INTO audit_events(actor_user_id,event_type,entity_type,entity_id,payload)
                    VALUES (NULL,'signal.post_execution_revision_applied','signal',:signal_id,CAST(:payload AS jsonb))
                    """
                ),
                {
                    "signal_id": signal_id,
                    "payload": json.dumps(
                        {
                            "source_revision_index": revision_index,
                            "same_message_revision": True,
                            "entry_reexecution_allowed": False,
                            "broker_actions_planned": len(actions),
                            "provider_tp_count": len(trade.take_profits),
                            "represented_tp_count": represented_tps,
                            "unrepresented_tp_count": max(0, len(trade.take_profits) - represented_tps),
                            "trade_action_created": False,
                        },
                        separators=(",", ":"),
                    ),
                },
            )
            session.commit()

        management_extracted = {
            **dict(gated.extracted),
            "management_actions": actions,
            "update_type": actions[0]["type"],
            "update_target": actions[0]["target"],
            "update_value": actions[0]["value"],
        }
        management_decision = replace(
            gated,
            decision="trade_update",
            action="apply_update",
            reason="post_execution_signal_revision",
            extracted=management_extracted,
        )
        self._store_decision(row["message_id"], revision_index, management_decision)
        return AiPipelineResult(
            True,
            "trade_update",
            "apply_update",
            signal_id,
            UUID(str(persisted_event_id)),
            management_decision.source,
            "post_execution_signal_revision",
        )


__all__ = ["CanonicalAiMessagePipeline", "explicit_management_without_ai"]
