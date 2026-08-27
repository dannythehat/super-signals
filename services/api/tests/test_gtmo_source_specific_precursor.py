from __future__ import annotations

from uuid import uuid4

from app.ai_message_supervisor import AiMessageDecision
from app.bare_gold_now_policy import PROFILE
from app.production_ai_pipeline import ProductionAiMessagePipeline


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _Session:
    def __init__(self, chat_id: int):
        self._chat_id = chat_id

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, *_args, **_kwargs):
        return _ScalarResult(self._chat_id)


class _SessionFactory:
    def __init__(self, chat_id: int):
        self._chat_id = chat_id

    def __call__(self):
        return _Session(self._chat_id)


class _CompleteSignalSupervisor:
    def __init__(self, *, double_lot: bool = False):
        self._double_lot = double_lot

    def decide_with_active_context(self, **_kwargs):
        return AiMessageDecision(
            decision="new_trade",
            action="execute",
            confidence=0.99,
            reason="semantic_complete_signal",
            extracted={
                "symbol": "XAUUSD",
                "side": "BUY",
                "order_type": "market",
                "entry_low": "4352",
                "entry_high": "4356",
                "stop_loss": "4346",
                "take_profits": ["4358", "4360", "4362", "4364"],
                "double_lot": self._double_lot,
                "update_type": None,
                "update_target": None,
                "update_value": None,
                "provider_claimed_pips": None,
            },
            model="test",
            response_id=None,
            latency_ms=1,
            source="openai",
            raw_text_sha256="x",
        )


class _CompleteSignalPipeline(ProductionAiMessagePipeline):
    def _source_profile(self, source_id):
        return None

    def _source_context(self, *, source_id, telegram_message_id):
        return "Known provider", []

    def _active_trade_context(self, *, source_id):
        return []


def _pipeline(chat_id: int) -> ProductionAiMessagePipeline:
    pipeline = object.__new__(ProductionAiMessagePipeline)
    pipeline._session_factory = _SessionFactory(chat_id)
    pipeline._supervisor = None
    return pipeline


def _complete_pipeline(chat_id: int, *, double_lot: bool = False) -> ProductionAiMessagePipeline:
    pipeline = object.__new__(_CompleteSignalPipeline)
    pipeline._session_factory = _SessionFactory(chat_id)
    pipeline._supervisor = _CompleteSignalSupervisor(double_lot=double_lot)
    return pipeline


def _decide(pipeline: ProductionAiMessagePipeline, text: str):
    return pipeline._decide(
        source_id=uuid4(),
        telegram_message_id=1,
        revision_index=0,
        raw_text=text,
        source_status="testing",
        reply_context=None,
        previous_text=None,
    )


def test_plain_gtmo_bare_buy_now_is_precursor_not_trade() -> None:
    result = _decide(_pipeline(-1001640332422), "Gold buy now")
    assert result.decision == "preparation"
    assert result.action == "ignore"
    assert result.reason == "provider_precursor_wait_for_structured_signal"
    assert result.extracted["side"] == "BUY"
    assert "execution_profile" not in result.extracted


def test_stylized_gtmo_mirror_has_same_precursor_grammar_if_reenabled() -> None:
    result = _decide(_pipeline(-1002068685216), "Gold sell now")
    assert result.decision == "preparation"
    assert result.action == "ignore"
    assert result.reason == "provider_precursor_wait_for_structured_signal"
    assert result.extracted["side"] == "SELL"


def test_fx_open_gold_buys_is_precursor_not_trade() -> None:
    result = _decide(_pipeline(-1001651583302), "OPEN GOLD BUYS NOW")
    assert result.decision == "preparation"
    assert result.action == "ignore"
    assert result.reason == "provider_precursor_wait_for_structured_signal"
    assert result.extracted["side"] == "BUY"


def test_fx_open_new_gold_sells_here_is_precursor_not_trade() -> None:
    result = _decide(_pipeline(-1001651583302), "OPEN NEW GOLD SELLS HERE.")
    assert result.decision == "preparation"
    assert result.action == "ignore"
    assert result.extracted["side"] == "SELL"


def test_identical_bare_now_from_other_provider_still_executes() -> None:
    result = _decide(_pipeline(-1009999999999), "Gold buy now")
    assert result.decision == "new_trade"
    assert result.action == "execute"
    assert result.reason == PROFILE
    assert result.extracted["execution_profile"] == PROFILE


def test_full_gtmo_signal_is_not_swallowed_by_precursor_policy() -> None:
    raw = (
        "Gold buy now 4356 - 4352\n\n"
        "SL: 4346\n\n"
        "TP: 4358\nTP: 4360\nTP: 4362\nTP: 4364\nTP: open"
    )
    result = _decide(_complete_pipeline(-1001640332422), raw)
    assert result.decision == "new_trade"
    assert result.action == "execute"
    assert result.extracted["entry_low"] == "4352"
    assert result.extracted["entry_high"] == "4356"
    assert result.extracted["stop_loss"] == "4346"


def test_full_fx_signal_is_not_swallowed_by_precursor_policy() -> None:
    raw = (
        "NEW TRADE IDEA\n\nXAUUSD BUY 4356\n\n"
        "TP 1 4358\nTP 2 4360\nTP 3 4364\n\nSL @ 4346"
    )
    result = _decide(_complete_pipeline(-1001651583302), raw)
    assert result.decision == "new_trade"
    assert result.action == "execute"
    assert result.extracted["stop_loss"] == "4346"


def test_locked_fx_risk_ignores_double_lot_wording() -> None:
    raw = (
        "NEW TRADE IDEA\n\nXAUUSD BUY 4356\n\n"
        "TP 1 4358\nTP 2 4360\nTP 3 4364\n\nSL @ 4346\nDOUBLE LOTSIZE"
    )
    result = _decide(_complete_pipeline(-1001651583302, double_lot=True), raw)
    assert result.decision == "new_trade"
    assert result.action == "execute"
    assert result.extracted["double_lot"] is False


def test_locked_gtmo_risk_ignores_double_lot_wording() -> None:
    raw = (
        "Gold buy now 4356 - 4352\nSL: 4346\n"
        "TP: 4358\nTP: 4360\nTP: 4362\nTP: 4364\nTP: open\nDOUBLE LOT"
    )
    result = _decide(_complete_pipeline(-1001640332422, double_lot=True), raw)
    assert result.extracted["double_lot"] is False
