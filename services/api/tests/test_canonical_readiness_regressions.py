from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

from app.ai_message_supervisor import AiMessageDecision
from app.performance_ledger_canonical import CanonicalPerformanceLedgerService
from app.telegram_publisher_canonical import _decimal_text, _render_root
from app.trading_management_canonical import (
    CanonicalTradingManagementService,
    _PROFITABLE_BROKER_IDS,
)
from app.v1_message_policy import apply_v1_message_policy


def _trade_update_decision(raw: str) -> AiMessageDecision:
    return AiMessageDecision(
        decision="trade_update",
        action="apply_update",
        confidence=0.99,
        reason="provider_update_fixture",
        extracted={
            "symbol": None,
            "side": None,
            "order_type": None,
            "entry_low": None,
            "entry_high": None,
            "stop_loss": None,
            "take_profits": [],
            "double_lot": False,
            "update_type": "close",
            "update_target": "all",
            "update_value": None,
            "management_actions": [
                {"type": "close", "target": "all", "value": None}
            ],
            "provider_claimed_pips": None,
        },
        model="fixture",
        response_id=None,
        latency_ms=1,
        source="openai",
        raw_text_sha256=sha256(raw.encode("utf-8")).hexdigest(),
    )


def test_sparse_signal_publication_never_crashes_decimal_rendering() -> None:
    assert _decimal_text(None) == "N/A"
    rendered = _render_root(
        {
            "symbol": "XAUUSD",
            "side": "BUY",
            "entry_low": None,
            "entry_high": None,
            "broker_entry": None,
            "stop_loss": None,
            "broker_stop_loss": None,
            "take_profits": [],
            "broker_take_profits": [],
            "has_open_runner": False,
            "risk_multiplier": 1,
        }
    )
    assert "<b>Entry</b>\nMarket" in rendered
    assert "<b>Stop Loss</b>\nN/A" in rendered


def test_provider_numeric_risk_free_stop_is_not_semantically_vetoed() -> None:
    surviving = SimpleNamespace(entry_index=2, entry_price=Decimal("4358"))
    selected = CanonicalTradingManagementService._select_layer_positions(
        (surviving,),
        "best_entry_risk_free_4357",
        side="BUY",
    )
    assert selected == (surviving,)


def test_tdc_close_profit_when_seen_is_broker_profit_qualified() -> None:
    raw = "+70 pips\n\nClose profit when you see it\n\nThis set up is now more risky"
    result = apply_v1_message_policy(_trade_update_decision(raw), raw_text=raw)
    assert result.decision == "trade_update"
    assert result.action == "apply_update"
    assert result.extracted.get("management_actions") == [
        {"type": "close", "target": "profitable_only", "value": None}
    ]
    assert result.extracted.get("update_type") == "close"
    assert result.extracted.get("update_target") == "profitable_only"


def test_profit_qualified_close_never_selects_a_losing_broker_position() -> None:
    winner = SimpleNamespace(broker_position_id="broker-win")
    loser = SimpleNamespace(broker_position_id="broker-loss")
    token = _PROFITABLE_BROKER_IDS.set(frozenset({"broker-win"}))
    try:
        selected = CanonicalTradingManagementService._select_layer_positions(
            (winner, loser),
            "profitable_only",
            side="BUY",
        )
    finally:
        _PROFITABLE_BROKER_IDS.reset(token)
    assert selected == (winner,)


def test_best_entry_still_running_is_state_not_an_extra_close_command() -> None:
    raw = (
        "+30 PIPS HIT 🔥\n\n"
        "RISK FREE 4357\n\n"
        "4357 SL TO BE\n"
        "4358 CLOSE +20\n"
        "4359 CLOSE +10\n\n"
        "TOTAL CLOSED PROFIT +30 PIPS AND BEST ENTRY STILL RUNNING WITH SL AT BE AT 4357"
    )
    result = apply_v1_message_policy(_trade_update_decision(raw), raw_text=raw)
    actions = result.extracted.get("management_actions") or []
    assert {"type": "close", "target": "entry_price_4358", "value": None} in actions
    assert {"type": "close", "target": "entry_price_4359", "value": None} in actions
    assert {
        "type": "edit_stop_loss",
        "target": "best_entry_risk_free_4357",
        "value": "4357",
    } in actions
    assert {"type": "close", "target": "all_but_best", "value": None} not in actions


class _Result:
    @staticmethod
    def first():
        return (1,)


class _Session:
    def __init__(self, statements: list[str]) -> None:
        self.statements = statements

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, statement, params=None):
        self.statements.append(str(statement))
        return _Result()

    def commit(self):
        return None


class _LedgerHarness:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self._session_factory = lambda: _Session(self.statements)

    @staticmethod
    def _mapped_positions(user_id):
        return []


def test_account_truth_sync_obeys_append_only_broker_deal_ledger() -> None:
    harness = _LedgerHarness()
    added = CanonicalPerformanceLedgerService._store_account_deals(
        harness,
        user_id=uuid4(),
        mt5_account_id=uuid4(),
        payloads=[
            {
                "id": "deal-1",
                "type": "DEAL_TYPE_BUY",
                "time": datetime.now(UTC).isoformat(),
                "symbol": "XAUUSD",
                "volume": 0.01,
                "price": 4400.0,
                "profit": 0,
                "commission": 0,
                "swap": 0,
            }
        ],
    )
    assert added == 1
    sql = "\n".join(harness.statements).upper()
    assert "ON CONFLICT" in sql
    assert "DO NOTHING" in sql
    assert "DO UPDATE" not in sql


def test_full_backfill_marker_explicitly_types_reused_postgres_binds() -> None:
    harness = _LedgerHarness()
    now = datetime.now(UTC)
    CanonicalPerformanceLedgerService._mark_full_backfill(
        harness,
        user_id=uuid4(),
        mt5_account_id=uuid4(),
        start_time=now,
        end_time=now,
        deal_count=3,
    )
    sql = "\n".join(harness.statements).upper()
    assert "CAST(:EVENT_TYPE AS VARCHAR)" in sql
    assert "CAST(:ACCOUNT_ID AS UUID)" in sql
    assert "CAST(:USER_ID AS UUID)" in sql
