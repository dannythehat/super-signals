from __future__ import annotations

import inspect
from datetime import timedelta

from app.ai_message_pipeline import AiMessagePipeline
from app.ai_message_supervisor import AiMessageDecision
from app.post_execution_edit_fidelity import _age_text
from app.telegram_publisher_day34_cutover import Day34CutoverTelegramPublisherManager
from app.v1_message_policy import apply_v1_message_policy


def _trade_decision(**extracted) -> AiMessageDecision:
    defaults = {
        "symbol": "XAUUSD",
        "side": "BUY",
        "order_type": "market",
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
    defaults.update(extracted)
    return AiMessageDecision(
        decision="new_trade",
        action="execute",
        confidence=0.99,
        reason="tdc_6605_fixture",
        extracted=defaults,
        model="test",
        response_id=None,
        latency_ms=1,
        source="openai",
        raw_text_sha256="fixture",
    )


def test_tdc_6605_complete_post_execution_edit_is_still_structurally_valid() -> None:
    """The completed edit must not be discarded merely because rev0 already traded."""
    previous = """Buy Gold Now

4401 - 4395

TP 4404

SL 4391"""
    complete = """Buy Gold Now

4401 - 4395

TP 4404
TP 4407
TP 4410

SL 4391"""
    decision = _trade_decision(
        entry_low="4395",
        entry_high="4401",
        stop_loss="4391",
        take_profits=["4404", "4407", "4410"],
    )
    result = apply_v1_message_policy(
        decision,
        raw_text=complete,
        is_edit=True,
        original_has_signal=True,
        previous_text=previous,
    )

    assert result.decision == "new_trade"
    assert result.action == "execute"
    assert result.extracted["stop_loss"] == "4391"
    assert result.extracted["take_profits"] == ["4404", "4407", "4410"]


def test_runtime_post_execution_wrapper_cannot_call_entry_executor() -> None:
    """Fresh same-message revisions become management, never a second entry call."""
    method = AiMessagePipeline._process_revision
    assert getattr(method, "_fresh_post_execution_revision_management", False) is True
    source = inspect.getsource(method)
    assert "entry_reexecution_allowed" in source
    assert "self._execution" not in source
    assert '"trade_update"' in source
    assert '"apply_update"' in source


def test_delayed_publication_marker_is_installed_and_reports_original_age() -> None:
    method = Day34CutoverTelegramPublisherManager._claim_next
    assert getattr(method, "_delayed_publication_marker", False) is True
    assert _age_text(timedelta(hours=4, minutes=10)) == "4h 10m"
