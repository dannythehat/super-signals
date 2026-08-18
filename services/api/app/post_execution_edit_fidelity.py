"""Fresh post-execution Telegram edit fidelity and delayed-publication clarity.

A provider may post an immediate command and then complete the *same Telegram message*
with the literal range/SL/TP seconds later. Once a broker position exists we must never
re-run entry execution, but the provider's fresh protection/target edit is still trading
truth. This override converts a structurally complete fresh same-message edit into a
management event for the already-mapped broker positions.

Safety invariants:
* never creates, reopens, resizes or adds a broker position;
* only same-message edits with a currently broker-backed open position are eligible;
* historical catch-up is evidence-only (freshness window prevents replay);
* explicit SL updates all existing open tranches;
* TPn updates only an already-existing local TPn tranche;
* extra provider TPs without an existing tranche are audited, never invented;
* paper/future-LIVE share the same downstream management engine.

The same module makes delayed Telegram mirror deliveries unmistakable. An old result
flushed after an outage/restart is labelled DELAYED with its original age instead of
appearing beside current broker activity as though it happened now.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

_EDIT_FRESHNESS = timedelta(minutes=5)
_PUBLICATION_DELAY = timedelta(minutes=5)
_installed = False


def _token(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _age_text(age: timedelta) -> str:
    seconds = max(0, int(age.total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{max(1, minutes)}m"


def _install_post_execution_revision_management() -> None:
    from app.ai_message_pipeline import AiMessagePipeline, AiPipelineResult
    from app.v1_message_policy import apply_v1_message_policy

    current = AiMessagePipeline._process_revision
    if getattr(current, "_fresh_post_execution_revision_management", False):
        return
    original = current

    def process_revision(
        self: Any,
        source_id: UUID,
        telegram_message_id: int,
        *,
        revision_index: int,
    ) -> AiPipelineResult:
        if revision_index <= 0:
            return original(
                self,
                source_id,
                telegram_message_id,
                revision_index=revision_index,
            )

        now = datetime.now(UTC)
        with self._session_factory() as session:
            candidate = session.execute(
                text(
                    """
                    SELECT
                        m.id AS message_id,
                        mr.edited_at,
                        sig.id AS signal_id,
                        EXISTS(
                            SELECT 1
                            FROM positions p
                            WHERE p.signal_id=sig.id
                              AND p.broker_position_id IS NOT NULL
                              AND p.status='open'
                        ) AS has_open_broker_position,
                        EXISTS(
                            SELECT 1
                            FROM ai_message_decisions d
                            WHERE d.message_id=m.id
                              AND d.revision_index=:revision_index
                        ) AS already_decided
                    FROM messages m
                    JOIN message_revisions mr
                      ON mr.message_id=m.id
                     AND mr.revision_index=:revision_index
                    LEFT JOIN signals sig ON sig.source_message_id=m.id
                    WHERE m.source_id=:source_id
                      AND m.telegram_message_id=:telegram_message_id
                    LIMIT 1
                    """
                ),
                {
                    "source_id": source_id,
                    "telegram_message_id": telegram_message_id,
                    "revision_index": revision_index,
                },
            ).mappings().first()

        if candidate is None or candidate["signal_id"] is None:
            return original(
                self,
                source_id,
                telegram_message_id,
                revision_index=revision_index,
            )
        if bool(candidate["already_decided"]):
            return original(
                self,
                source_id,
                telegram_message_id,
                revision_index=revision_index,
            )
        if not bool(candidate["has_open_broker_position"]):
            return original(
                self,
                source_id,
                telegram_message_id,
                revision_index=revision_index,
            )

        edited_at = candidate["edited_at"]
        if not isinstance(edited_at, datetime):
            return original(
                self,
                source_id,
                telegram_message_id,
                revision_index=revision_index,
            )
        if edited_at.tzinfo is None:
            edited_at = edited_at.replace(tzinfo=UTC)
        else:
            edited_at = edited_at.astimezone(UTC)
        if now - edited_at > _EDIT_FRESHNESS:
            return original(
                self,
                source_id,
                telegram_message_id,
                revision_index=revision_index,
            )

        with self._session_factory() as session:
            row = self._load_revision(
                session,
                source_id=source_id,
                telegram_message_id=telegram_message_id,
                revision_index=revision_index,
            )
            if row is None:
                return original(
                    self,
                    source_id,
                    telegram_message_id,
                    revision_index=revision_index,
                )
            previous_text = self._previous_text(
                session,
                row["message_id"],
                revision_index,
            )
            reply_context = self._reply_context(
                session,
                source_id,
                row["raw_payload"],
            )

        raw_text = str(row["raw_text"] or "")
        if not raw_text.strip() or (previous_text is not None and raw_text == previous_text):
            return original(
                self,
                source_id,
                telegram_message_id,
                revision_index=revision_index,
            )

        # A same-message edit is a replacement candidate for the same logical trade.
        # Run the normal semantic interpreter, then the exact same V1 mechanical gate.
        semantic = self._decide(
            source_id=source_id,
            telegram_message_id=telegram_message_id,
            revision_index=revision_index,
            raw_text=raw_text,
            source_status=str(row["source_status"]),
            reply_context=reply_context,
            previous_text=previous_text,
        )
        candidate_trade = replace(
            semantic,
            decision="new_trade",
            action="execute",
        )
        gated = apply_v1_message_policy(
            candidate_trade,
            raw_text=raw_text,
            is_edit=True,
            original_has_signal=True,
            previous_text=previous_text,
        )

        if gated.decision != "new_trade" or gated.action != "execute":
            # Incomplete intermediate edits remain evidence only. A later complete edit
            # can still pass this path within the freshness window.
            self._store_decision(row["message_id"], revision_index, gated)
            return AiPipelineResult(
                True,
                gated.decision,
                gated.action,
                UUID(str(candidate["signal_id"])),
                None,
                gated.source,
                gated.reason,
            )

        try:
            trade = self._signals._parse_extracted(gated.extracted)
        except ValueError:
            self._store_decision(row["message_id"], revision_index, gated)
            return AiPipelineResult(
                True,
                "non_actionable",
                "skip",
                UUID(str(candidate["signal_id"])),
                None,
                gated.source,
                "post_execution_revision_incomplete",
            )

        signal_id = UUID(str(candidate["signal_id"]))
        with self._session_factory() as session:
            existing_tp_indices = {
                int(value)
                for value in session.execute(
                    text(
                        """
                        SELECT DISTINCT tp_index
                        FROM positions
                        WHERE signal_id=:signal_id
                          AND broker_position_id IS NOT NULL
                          AND status='open'
                        """
                    ),
                    {"signal_id": signal_id},
                ).scalars().all()
            }

            actions: list[dict[str, str | None]] = [
                {
                    "type": "edit_stop_loss",
                    "target": "all",
                    "value": _token(trade.stop_loss),
                }
            ]
            represented_tps = 0
            for index, target in enumerate(trade.take_profits, start=1):
                if index not in existing_tp_indices:
                    continue
                represented_tps += 1
                actions.append(
                    {
                        "type": "edit_take_profit",
                        "target": f"tp{index}",
                        "value": _token(target),
                    }
                )

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
            fingerprint = sha256(
                json.dumps(
                    fingerprint_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()

            # Canonical provider truth advances to the completed edit. Existing broker
            # execution rows remain untouched; this never re-enters or resizes exposure.
            session.execute(
                text(
                    """
                    UPDATE signals
                    SET source_revision_index=:revision_index,
                        source_posted_at=:edited_at,
                        signal_fingerprint=:fingerprint,
                        symbol=:symbol,
                        side=:side,
                        order_type=:order_type,
                        entry_low=:entry_low,
                        entry_high=:entry_high,
                        stop_loss=:stop_loss,
                        take_profits=CAST(:take_profits AS jsonb),
                        has_open_runner=:has_open_runner,
                        parser_status='accepted',
                        skip_reason=NULL,
                        risk_multiplier=:risk_multiplier,
                        original_text=:original_text,
                        updated_at=now()
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
                    ) VALUES (
                        :signal_id,:message_id,:revision_index,'canonical',:fingerprint
                    ) ON CONFLICT (message_id,revision_index) DO NOTHING
                    """
                ),
                {
                    "signal_id": signal_id,
                    "message_id": row["message_id"],
                    "revision_index": revision_index,
                    "fingerprint": fingerprint,
                },
            )

            event_id = uuid4()
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
            session.execute(
                text(
                    """
                    INSERT INTO signal_lifecycle_events(
                        id,signal_id,source_message_id,source_revision_index,
                        event_type,event_key,origin,rendered_text,pips,
                        aggregate_result,occurred_at
                    ) VALUES (
                        :id,:signal_id,:message_id,:revision_index,
                        'signal_revision',:event_key,'provider_update',
                        'Provider completed trade details in the original Telegram message.',
                        NULL,CAST(:aggregate AS jsonb),:occurred_at
                    )
                    ON CONFLICT (event_key) DO NOTHING
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
                text(
                    "SELECT id FROM signal_lifecycle_events WHERE event_key=:event_key"
                ),
                {"event_key": event_key},
            ).scalar_one()
            session.execute(
                text(
                    """
                    INSERT INTO audit_events(
                        actor_user_id,event_type,entity_type,entity_id,payload
                    ) VALUES (
                        NULL,'signal.revised_after_execution.ai_supervisor',
                        'signal',:signal_id,CAST(:payload AS jsonb)
                    )
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
                            "unrepresented_tp_count": max(
                                0,
                                len(trade.take_profits) - represented_tps,
                            ),
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

    process_revision._fresh_post_execution_revision_management = True  # type: ignore[attr-defined]
    AiMessagePipeline._process_revision = process_revision


def _install_delayed_publication_marker() -> None:
    from app.telegram_publisher import PublicationAttempt
    from app.telegram_publisher_day20 import LifecyclePublicationAttempt
    from app.telegram_publisher_day34_cutover import Day34CutoverTelegramPublisherManager

    current = Day34CutoverTelegramPublisherManager._claim_next
    if getattr(current, "_delayed_publication_marker", False):
        return
    original = current

    def claim_next(self: Any) -> Any:
        attempt = original(self)
        if attempt is None or not isinstance(attempt, PublicationAttempt):
            return attempt

        with self._session_factory() as session:
            event_time = session.execute(
                text(
                    """
                    SELECT COALESCE(ev.occurred_at,sig.source_posted_at,pub.created_at)
                    FROM telegram_publications pub
                    JOIN signals sig ON sig.id=pub.signal_id
                    LEFT JOIN signal_lifecycle_events ev ON ev.id=pub.lifecycle_event_id
                    WHERE pub.id=:publication_id
                    """
                ),
                {"publication_id": attempt.publication_id},
            ).scalar_one_or_none()

        if not isinstance(event_time, datetime):
            return attempt
        if event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=UTC)
        else:
            event_time = event_time.astimezone(UTC)
        age = datetime.now(UTC) - event_time
        if age <= _PUBLICATION_DELAY or attempt.text.startswith("⏱ DELAYED"):
            return attempt

        marked = f"⏱ DELAYED UPDATE · original event {_age_text(age)} earlier\n{attempt.text}"
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE telegram_publications
                    SET rendered_text=:rendered_text,updated_at=now()
                    WHERE id=:publication_id AND status='sending'
                    """
                ),
                {
                    "publication_id": attempt.publication_id,
                    "rendered_text": marked,
                },
            )
            session.commit()

        if isinstance(attempt, LifecyclePublicationAttempt):
            return LifecyclePublicationAttempt(
                publication_id=attempt.publication_id,
                signal_id=attempt.signal_id,
                text=marked,
                reply_to_message_id=attempt.reply_to_message_id,
            )
        return PublicationAttempt(
            publication_id=attempt.publication_id,
            signal_id=attempt.signal_id,
            text=marked,
        )

    claim_next._delayed_publication_marker = True  # type: ignore[attr-defined]
    Day34CutoverTelegramPublisherManager._claim_next = claim_next


def install_post_execution_edit_fidelity() -> None:
    global _installed
    if _installed:
        return
    _install_post_execution_revision_management()
    _install_delayed_publication_marker()
    _installed = True


__all__ = ["install_post_execution_edit_fidelity"]
