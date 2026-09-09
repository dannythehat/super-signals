"""Provider Intelligence Day 21 continuous full-chain research runtime.

Day 21 does not create a second scheduler and has no broker authority. It creates one
immutable anchor for every accepted shadow signal, then append-only stage events as
PIT-safe evidence becomes available. Missing evidence is explicit; history is never
rewritten to make a later stage look available at signal time.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.provider_day16_veto_counterfactual import (
    MODEL_VERSION as DAY16_MODEL_VERSION,
    RULE_VERSION as DAY16_RULE_VERSION,
    STATISTICAL_AUTHORITY as DAY16_STATISTICAL_AUTHORITY,
    decide_veto_counterfactual,
)
from app.provider_day17_confidence_sizing import (
    LIVE_VARIABLE_SIZING_ALLOWED,
    PAPER_VARIABLE_SIZING_ALLOWED,
    VARIABLE_SIZING_AUTHORITY,
)
from app.provider_day18_combined_book import (
    ACCOUNT_MODE_REQUIRED,
    PORTFOLIO_AUTHORITY,
    PRODUCTION_PORTFOLIO_AUTHORITY,
)
from app.provider_day19_explainer_budget import EvidenceState, placed_trade_explanation
from app.provider_day20_management_counterfactual import (
    LIVE_MANAGEMENT_ALLOWED,
    MANAGEMENT_EFFICACY,
    PAPER_MANAGEMENT_ALLOWED,
)

logger = logging.getLogger(__name__)

CONTRACT_VERSION = "provider_day21_chain_v1"
FORWARD_ANCHOR_MAX_DELAY_SECONDS = 300
_DEFAULT_BATCH_LIMIT = 500


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("provider_day21_timezone_required")
    return value.astimezone(UTC)


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return _utc(value).isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


class ProviderDay21ChainResolver:
    """Append PIT-safe research evidence for accepted shadow-provider signals."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        batch_limit: int = _DEFAULT_BATCH_LIMIT,
    ) -> None:
        if batch_limit <= 0 or batch_limit > 1000:
            raise ValueError("provider_day21_batch_limit_invalid")
        self._session_factory = session_factory
        self._batch_limit = batch_limit

    @staticmethod
    def _insert_event(
        session: Session,
        *,
        chain_id: UUID,
        signal_id: UUID,
        stage: str,
        event_key: str,
        status: str,
        evidence_as_of: datetime | None,
        payload: dict[str, Any],
    ) -> bool:
        safe_payload = _jsonable(payload)
        digest = _digest(safe_payload)
        inserted = session.execute(
            text(
                """
                INSERT INTO provider_day21_chain_events(
                    id,chain_id,signal_id,stage,event_key,status,evidence_as_of,
                    evidence_payload,evidence_digest,research_only,
                    live_money_execution_allowed,paper_execution_allowed,
                    live_variable_sizing_allowed,paper_variable_sizing_allowed,
                    live_management_allowed,paper_management_allowed,public_broadcast_allowed
                ) VALUES (
                    :id,:chain_id,:signal_id,:stage,:event_key,:status,:evidence_as_of,
                    CAST(:payload AS jsonb),:digest,true,false,false,false,false,false,false,false
                )
                ON CONFLICT (signal_id,stage,event_key) DO NOTHING
                RETURNING id
                """
            ),
            {
                "id": uuid4(),
                "chain_id": chain_id,
                "signal_id": signal_id,
                "stage": stage,
                "event_key": event_key,
                "status": status,
                "evidence_as_of": evidence_as_of,
                "payload": _canonical(safe_payload),
                "digest": digest,
            },
        ).scalar_one_or_none()
        return inserted is not None

    def _sync_anchors(self, session: Session) -> int:
        rows = session.execute(
            text(
                """
                WITH clock AS (SELECT now() AS anchored_at)
                INSERT INTO provider_day21_signal_chains(
                    signal_id,source_id,message_id,signal_posted_at,anchored_at,
                    anchor_delay_seconds,forward_decision_eligible,contract_version,
                    research_only,live_money_execution_allowed,paper_execution_allowed,
                    live_variable_sizing_allowed,paper_variable_sizing_allowed,
                    live_management_allowed,paper_management_allowed,public_broadcast_allowed
                )
                SELECT s.id,s.source_id,s.source_message_id,s.source_posted_at,c.anchored_at,
                       GREATEST(EXTRACT(EPOCH FROM (c.anchored_at-s.source_posted_at)),0),
                       (s.source_posted_at <= c.anchored_at
                        AND c.anchored_at-s.source_posted_at <= interval '5 minutes'),
                       :contract_version,true,false,false,false,false,false,false,false
                FROM signals s
                JOIN sources src ON src.id=s.source_id
                CROSS JOIN clock c
                WHERE src.status='shadow' AND s.parser_status='accepted'
                ON CONFLICT (signal_id) DO NOTHING
                RETURNING id
                """
            ),
            {"contract_version": CONTRACT_VERSION},
        ).scalars().all()
        return len(rows)

    def _chains_needing_work(self, session: Session) -> list[dict[str, Any]]:
        rows = session.execute(
            text(
                """
                SELECT c.id AS chain_id,c.signal_id,c.source_id,c.message_id,
                       c.signal_posted_at,c.anchored_at,c.anchor_delay_seconds,
                       c.forward_decision_eligible,s.side
                FROM provider_day21_signal_chains c
                JOIN signals s ON s.id=c.signal_id
                WHERE (
                    SELECT count(DISTINCT e.stage)
                    FROM provider_day21_chain_events e
                    WHERE e.signal_id=c.signal_id
                      AND e.stage IN (
                          'enrollment','provider_profile','aidy_context','day16_veto',
                          'day17_sizing','day18_book','day19_explanation','day20_management'
                      )
                ) < 8
                OR (
                    EXISTS (
                        SELECT 1 FROM shadow_trades t
                        WHERE t.signal_id=c.signal_id
                          AND t.status IN ('closed','cancelled','missed')
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM provider_day21_chain_events e
                        WHERE e.signal_id=c.signal_id AND e.stage='outcome_feedback'
                    )
                )
                ORDER BY c.signal_posted_at DESC,c.id DESC
                LIMIT :limit
                """
            ),
            {"limit": self._batch_limit},
        ).mappings().all()
        return [dict(row) for row in rows]

    @staticmethod
    def _event_exists(session: Session, signal_id: UUID, stage: str) -> bool:
        return bool(
            session.execute(
                text(
                    "SELECT 1 FROM provider_day21_chain_events "
                    "WHERE signal_id=:signal_id AND stage=:stage LIMIT 1"
                ),
                {"signal_id": signal_id},
            ).scalar_one_or_none()
        )

    @staticmethod
    def _enrollment(session: Session, signal_id: UUID) -> dict[str, Any] | None:
        row = session.execute(
            text(
                """
                SELECT status,reason,provider_signal_posted_at,enrollment_observed_at,
                       quote_mode,entry_delay_ms,research_only,live_money_execution_allowed
                FROM provider_shadow_enrollment_audit WHERE signal_id=:signal_id
                """
            ),
            {"signal_id": signal_id},
        ).mappings().first()
        if row is None or str(row["status"]) == "pending":
            return None
        return dict(row)

    @staticmethod
    def _profile(session: Session, signal_id: UUID) -> dict[str, Any] | None:
        row = session.execute(
            text(
                """
                SELECT provider_profile_pit_status,provider_profile_version_id,
                       provider_profile_version_no,provider_profile_effective_at,
                       provider_style,score_exclusion_reason
                FROM shadow_trades
                WHERE signal_id=:signal_id
                ORDER BY entry_index ASC LIMIT 1
                """
            ),
            {"signal_id": signal_id},
        ).mappings().first()
        return None if row is None else dict(row)

    @staticmethod
    def _context(session: Session, signal_id: UUID) -> tuple[str, dict[str, Any]] | None:
        attached = session.execute(
            text(
                """
                SELECT id,aidy_requested_as_of_utc,aidy_context_as_of_utc,
                       aidy_context_lag_seconds,aidy_context_hash,aidy_snapshot_id,
                       aidy_snapshot_digest,attachment_digest,contract_version,created_at
                FROM provider_signal_context_attachments
                WHERE signal_id=:signal_id LIMIT 1
                """
            ),
            {"signal_id": signal_id},
        ).mappings().first()
        if attached is not None:
            return "attached", dict(attached)
        missed = session.execute(
            text(
                """
                SELECT id,reason,signal_posted_at,contract_version,created_at,response_payload
                FROM provider_signal_context_terminal_misses
                WHERE signal_id=:signal_id LIMIT 1
                """
            ),
            {"signal_id": signal_id},
        ).mappings().first()
        if missed is not None:
            return "terminal_miss", dict(missed)
        return None

    def _persist_day16_forward_decision(
        self,
        session: Session,
        *,
        chain: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        signal_id = UUID(str(chain["signal_id"]))
        source_id = UUID(str(chain["source_id"]))
        posted_at = _utc(chain["signal_posted_at"])
        existing = session.execute(
            text(
                """
                SELECT id,research_action,reason,input_digest,decided_at
                FROM provider_veto_counterfactual_decisions
                WHERE signal_id=:signal_id AND rule_version=:rule_version LIMIT 1
                """
            ),
            {"signal_id": signal_id, "rule_version": DAY16_RULE_VERSION},
        ).mappings().first()
        if existing is not None:
            return "forward_decision_existing", dict(existing)

        decided_at = datetime.now(UTC)
        decision = decide_veto_counterfactual(
            signal_id=str(signal_id),
            source_id=str(source_id),
            signal_posted_at=posted_at,
            decided_at=decided_at,
            evidence=[],
        )
        decision_id = uuid4()
        inserted = session.execute(
            text(
                """
                INSERT INTO provider_veto_counterfactual_decisions(
                    id,signal_id,source_id,signal_posted_at,decided_at,model_version,
                    rule_version,research_action,reason,selected_hypothesis_id,
                    selected_shrunken_effect_r,selected_cell_oos_n,
                    selected_complement_oos_n,input_digest,telegram_broadcast_allowed,
                    executable,statistical_authority,research_only,live_money_execution_allowed
                ) VALUES (
                    :id,:signal_id,:source_id,:signal_posted_at,:decided_at,:model_version,
                    :rule_version,:action,:reason,NULL,NULL,NULL,NULL,:input_digest,false,
                    false,:authority,true,false
                )
                ON CONFLICT (signal_id,rule_version) DO NOTHING RETURNING id
                """
            ),
            {
                "id": decision_id,
                "signal_id": signal_id,
                "source_id": source_id,
                "signal_posted_at": posted_at,
                "decided_at": decision.decided_at,
                "model_version": DAY16_MODEL_VERSION,
                "rule_version": DAY16_RULE_VERSION,
                "action": decision.research_action,
                "reason": decision.reason,
                "input_digest": decision.input_digest,
                "authority": DAY16_STATISTICAL_AUTHORITY,
            },
        ).scalar_one_or_none()
        return (
            "forward_decision_recorded" if inserted is not None else "forward_decision_existing",
            {
                "decision_id": str(inserted or decision_id),
                "research_action": decision.research_action,
                "reason": decision.reason,
                "input_digest": decision.input_digest,
                "decided_at": decision.decided_at,
                "day13_evidence_used": 0,
                "day13_evidence_policy": (
                    "automatic matching disabled because Day13 duration_bucket is "
                    "realized_descriptive_post_entry and cannot be known at signal time"
                ),
            },
        )

    def _process_chain(self, session: Session, chain: dict[str, Any]) -> int:
        inserted = 0
        chain_id = UUID(str(chain["chain_id"]))
        signal_id = UUID(str(chain["signal_id"]))
        posted_at = _utc(chain["signal_posted_at"])

        enrollment = self._enrollment(session, signal_id)
        if enrollment is not None and not self._event_exists(session, signal_id, "enrollment"):
            if self._insert_event(
                session,
                chain_id=chain_id,
                signal_id=signal_id,
                stage="enrollment",
                event_key="enrollment_v1",
                status=str(enrollment["status"]),
                evidence_as_of=enrollment.get("enrollment_observed_at"),
                payload={"contract_version": CONTRACT_VERSION, **enrollment},
            ):
                inserted += 1

        if enrollment is None:
            return inserted

        excluded = str(enrollment["status"]) == "excluded"
        profile = self._profile(session, signal_id)
        if not self._event_exists(session, signal_id, "provider_profile"):
            if excluded:
                profile_status = "not_applicable_enrollment_excluded"
                profile_payload = {"enrollment_reason": enrollment.get("reason")}
                profile_as_of = posted_at
            elif profile is None or str(profile.get("provider_profile_pit_status")) != "resolved":
                return inserted
            else:
                profile_status = "resolved"
                profile_payload = profile
                profile_as_of = profile.get("provider_profile_effective_at")
            if self._insert_event(
                session,
                chain_id=chain_id,
                signal_id=signal_id,
                stage="provider_profile",
                event_key="profile_v1",
                status=profile_status,
                evidence_as_of=profile_as_of,
                payload=profile_payload,
            ):
                inserted += 1

        context_result = self._context(session, signal_id)
        if not self._event_exists(session, signal_id, "aidy_context"):
            if excluded:
                context_status = "not_applicable_enrollment_excluded"
                context_payload = {"enrollment_reason": enrollment.get("reason")}
                context_as_of = posted_at
            elif context_result is None:
                return inserted
            else:
                context_status, context_payload = context_result
                context_as_of = (
                    context_payload.get("aidy_context_as_of_utc")
                    if context_status == "attached"
                    else context_payload.get("signal_posted_at")
                )
            if self._insert_event(
                session,
                chain_id=chain_id,
                signal_id=signal_id,
                stage="aidy_context",
                event_key="context_v1",
                status=context_status,
                evidence_as_of=context_as_of,
                payload=context_payload,
            ):
                inserted += 1

        if not self._event_exists(session, signal_id, "day16_veto"):
            if excluded:
                day16_status = "not_applicable_enrollment_excluded"
                day16_payload = {"research_action": "none"}
            elif not bool(chain["forward_decision_eligible"]):
                day16_status = "historical_unscored_no_forward_decision"
                day16_payload = {
                    "anchor_delay_seconds": float(chain["anchor_delay_seconds"]),
                    "max_forward_anchor_delay_seconds": FORWARD_ANCHOR_MAX_DELAY_SECONDS,
                    "research_action": "none",
                }
            else:
                day16_status, day16_payload = self._persist_day16_forward_decision(
                    session, chain=chain
                )
            if self._insert_event(
                session,
                chain_id=chain_id,
                signal_id=signal_id,
                stage="day16_veto",
                event_key="day16_v1",
                status=day16_status,
                evidence_as_of=posted_at,
                payload=day16_payload,
            ):
                inserted += 1

        if not self._event_exists(session, signal_id, "day17_sizing"):
            status = (
                "not_applicable_enrollment_excluded"
                if excluded
                else "dormant_waiting_for_calibrated_forward_confidence"
            )
            payload = {
                "variable_sizing_authority": VARIABLE_SIZING_AUTHORITY,
                "live_variable_sizing_allowed": LIVE_VARIABLE_SIZING_ALLOWED,
                "paper_variable_sizing_allowed": PAPER_VARIABLE_SIZING_ALLOWED,
                "confidence_used_for_execution": False,
            }
            if self._insert_event(
                session,
                chain_id=chain_id,
                signal_id=signal_id,
                stage="day17_sizing",
                event_key="day17_v1",
                status=status,
                evidence_as_of=posted_at,
                payload=payload,
            ):
                inserted += 1

        if not self._event_exists(session, signal_id, "day18_book"):
            status = (
                "not_applicable_enrollment_excluded"
                if excluded
                else "dormant_no_production_portfolio_authority"
            )
            payload = {
                "portfolio_authority": PORTFOLIO_AUTHORITY,
                "production_portfolio_authority": PRODUCTION_PORTFOLIO_AUTHORITY,
                "account_mode_required": ACCOUNT_MODE_REQUIRED,
                "broker_read_performed_by_day21": False,
            }
            if self._insert_event(
                session,
                chain_id=chain_id,
                signal_id=signal_id,
                stage="day18_book",
                event_key="day18_v1",
                status=status,
                evidence_as_of=posted_at,
                payload=payload,
            ):
                inserted += 1

        if not self._event_exists(session, signal_id, "day19_explanation"):
            explanation = placed_trade_explanation(
                placed=False,
                direction=str(chain["side"]),
                market_context="private Provider Lab shadow research",
                evidence=EvidenceState(
                    forward_n=0,
                    statistical_status="WAITING-FOR-FORWARD-EVIDENCE",
                    confidence=None,
                ),
                risk_note="research only",
            )
            if self._insert_event(
                session,
                chain_id=chain_id,
                signal_id=signal_id,
                stage="day19_explanation",
                event_key="day19_v1",
                status="shadow_internal_only",
                evidence_as_of=posted_at,
                payload=explanation,
            ):
                inserted += 1

        if not self._event_exists(session, signal_id, "day20_management"):
            status = (
                "not_applicable_enrollment_excluded"
                if excluded
                else "waiting_for_genuine_forward_management_evidence"
            )
            payload = {
                "management_efficacy": MANAGEMENT_EFFICACY,
                "live_management_allowed": LIVE_MANAGEMENT_ALLOWED,
                "paper_management_allowed": PAPER_MANAGEMENT_ALLOWED,
                "management_decision_invented": False,
            }
            if self._insert_event(
                session,
                chain_id=chain_id,
                signal_id=signal_id,
                stage="day20_management",
                event_key="day20_policy_v1",
                status=status,
                evidence_as_of=posted_at,
                payload=payload,
            ):
                inserted += 1

        terminal = session.execute(
            text(
                """
                SELECT status,close_reason,quality_r_multiple,pnl_percent,realized_percent,
                       opened_at,closed_at,updated_at,score_exclusion_reason
                FROM shadow_trades
                WHERE signal_id=:signal_id
                  AND status IN ('closed','cancelled','missed')
                ORDER BY entry_index ASC LIMIT 1
                """
            ),
            {"signal_id": signal_id},
        ).mappings().first()
        if terminal is not None and not self._event_exists(session, signal_id, "outcome_feedback"):
            terminal_payload = dict(terminal)
            if self._insert_event(
                session,
                chain_id=chain_id,
                signal_id=signal_id,
                stage="outcome_feedback",
                event_key="terminal_shadow_outcome_v1",
                status=str(terminal["status"]),
                evidence_as_of=terminal.get("closed_at") or terminal.get("updated_at"),
                payload=terminal_payload,
            ):
                inserted += 1

        return inserted

    def resolve_once(self) -> tuple[int, int]:
        processed = 0
        failures = 0
        with self._session_factory() as session:
            processed += self._sync_anchors(session)
            session.commit()

        with self._session_factory() as session:
            chains = self._chains_needing_work(session)

        for chain in chains:
            try:
                with self._session_factory() as session:
                    processed += self._process_chain(session, chain)
                    session.commit()
            except Exception:
                failures += 1
                logger.exception(
                    "Provider Day21 chain failed safely signal_id=%s",
                    chain.get("signal_id"),
                )
        return processed, failures


__all__ = [
    "CONTRACT_VERSION",
    "FORWARD_ANCHOR_MAX_DELAY_SECONDS",
    "ProviderDay21ChainResolver",
]
