"""Use deterministic trade evidence before paying an AI supervisor.

OpenAI is an ambiguity resolver in Super Signals, not the first-line parser. Literal
broker-management commands, already validated legacy trade parses and messages already
classified as chatter can be decided locally. Ambiguous provider language still falls
through to the existing source-aware OpenAI supervisor.
"""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from typing import Any

from sqlalchemy import text

from app.ai_message_supervisor import AiMessageDecision


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


def explicit_management_without_ai(raw_text: str) -> AiMessageDecision | None:
    """Return a local decision for a mechanically explicit broker instruction."""
    # Import at call time so the production literal-management override installed by
    # app.__init__ is the exact grammar used here and by V1 policy.
    import app.day27_management_policy as day27

    policy = day27.extract_day27_management_actions(raw_text or "")
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
        model="deterministic-no-ai-v2",
        response_id=None,
        latency_ms=0,
        source="deterministic_no_ai",
        raw_text_sha256=sha256((raw_text or "").encode("utf-8")).hexdigest(),
    )


def _classified_chatter_without_ai(
    pipeline: Any,
    *,
    source_id: Any,
    telegram_message_id: int,
    revision_index: int,
    raw_text: str,
) -> AiMessageDecision | None:
    """Trust only an existing deterministic chatter classification after management check."""
    with pipeline._session_factory() as session:
        row = session.execute(
            text(
                """
                SELECT mc.classification, mc.decision_status
                FROM messages AS m
                JOIN message_classifications AS mc
                  ON mc.message_id = m.id
                 AND mc.revision_index = :revision_index
                WHERE m.source_id = :source_id
                  AND m.telegram_message_id = :telegram_message_id
                LIMIT 1
                """
            ),
            {
                "source_id": source_id,
                "telegram_message_id": telegram_message_id,
                "revision_index": revision_index,
            },
        ).mappings().first()

    if row is None or str(row["classification"] or "") != "chatter":
        return None
    if str(row["decision_status"] or "") not in {"classified", "ignored"}:
        return None

    return AiMessageDecision(
        decision="chatter",
        action="ignore",
        confidence=1.0,
        reason="deterministic_classifier_chatter_no_ai",
        extracted=_empty_extracted(),
        model="deterministic-no-ai-v2",
        response_id=None,
        latency_ms=0,
        source="deterministic_no_ai",
        raw_text_sha256=sha256((raw_text or "").encode("utf-8")).hexdigest(),
    )


def _wrap_decider(cls: type[Any]) -> None:
    original = cls._decide
    if getattr(original, "_deterministic_first_ai_installed", False):
        return

    def wrapped(
        self: Any,
        *,
        source_id: Any,
        telegram_message_id: int,
        revision_index: int,
        raw_text: str,
        source_status: str,
        reply_context: str | None,
        previous_text: str | None,
    ) -> AiMessageDecision:
        # Safety priority: explicit close/SL/BE/partial/cancel commands never depend on
        # an AI call or AI classification. V1 policy still resolves target/linking.
        management = explicit_management_without_ai(raw_text)
        if management is not None:
            return management

        # Known parser formats already passed deterministic validation. The existing V1
        # policy runs after this decision and re-verifies literal values/direction before
        # a signal can become executable.
        deterministic = self._deterministic_fallback(
            source_id=source_id,
            telegram_message_id=telegram_message_id,
            revision_index=revision_index,
            raw_text=raw_text,
        )
        if deterministic.decision == "new_trade" and deterministic.action == "execute":
            return replace(
                deterministic,
                model="deterministic-no-ai-v2",
                source="deterministic_no_ai",
                reason="deterministic_known_trade_no_ai",
            )

        # Check chatter only after management. This prevents a weak chatter rule from
        # ever swallowing a literal broker instruction while eliminating the largest
        # historical source of unnecessary OpenAI spend.
        chatter = _classified_chatter_without_ai(
            self,
            source_id=source_id,
            telegram_message_id=telegram_message_id,
            revision_index=revision_index,
            raw_text=raw_text,
        )
        if chatter is not None:
            return chatter

        return original(
            self,
            source_id=source_id,
            telegram_message_id=telegram_message_id,
            revision_index=revision_index,
            raw_text=raw_text,
            source_status=source_status,
            reply_context=reply_context,
            previous_text=previous_text,
        )

    wrapped._deterministic_first_ai_installed = True  # type: ignore[attr-defined]
    cls._decide = wrapped


def install_deterministic_first_ai() -> None:
    """Install on both the base and production source-aware decision pipelines."""
    from app.ai_message_pipeline import AiMessagePipeline
    from app.ai_source_aware_pipeline import SourceAwareAiMessagePipeline

    _wrap_decider(AiMessagePipeline)
    _wrap_decider(SourceAwareAiMessagePipeline)


__all__ = ["explicit_management_without_ai", "install_deterministic_first_ai"]
