from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import app.performance_account_truth_override as account_truth
import app.telegram_publisher as publisher
from app.ai_message_supervisor import AiMessageDecision
from app.aug18_readiness_cleanup import install_aug18_readiness_cleanup
from app.paper_critical_management_v2 import PaperCriticalManagementV2
from app.v1_message_policy import apply_v1_message_policy


# Exercise the same runtime corrections production installs during application startup.
install_aug18_readiness_cleanup()


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
    assert publisher._decimal_text(None) == "N/A"
    row = {
        "symbol": "XAUUSD",
        "side": "BUY",
        "entry_price": None,
        "stop_loss": None,
        "take_profits": [],
        "has_open_runner": False,
        "risk_multiplier": 1,
    }
    rendered = publisher.render_signal_post(row)
    assert "Entry: N/A" in rendered
    assert "Stop Loss: N/A" in rendered


def test_provider_numeric_risk_free_stop_is_not_semantically_vetoed() -> None:
    """Regression for TDC 6665: literal 4357 must target the surviving best layer."""
    surviving = SimpleNamespace(entry_index=2, entry_price=Decimal("4358"))
    selected = PaperCriticalManagementV2._select_layer_positions(
        (surviving,),
        "best_entry_risk_free_4357",
        side="BUY",
    )
    assert selected == (surviving,)


def test_tdc_close_profit_when_seen_never_becomes_unconditional_loss_close() -> None:
    """Regression for TDC 6607, which historically closed a losing mapped trade."""
    raw = "+70 pips\n\nClose profit when you see it\n\nThis set up is now more risky"
    result = apply_v1_message_policy(_trade_update_decision(raw), raw_text=raw)

    assert result.decision == "trade_update"
    assert result.action == "ignore"
    assert result.extracted.get("management_actions") == []
    assert result.extracted.get("update_type") is None


def test_best_entry_still_running_is_state_not_an_extra_close_command() -> None:
    """Regression for TDC 6665 revision 3: preserve explicit closes, invent none."""
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
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, statement, params=None):
        self.statements.append(str(statement))
        return _Result()

    def commit(self):
        self.committed = True


class _LedgerHarness:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self._session_factory = lambda: _Session(self.statements)

    @staticmethod
    def _mapped_positions(user_id):
        return []


def test_account_truth_sync_obeys_append_only_broker_deal_ledger() -> None:
    harness = _LedgerHarness()
    added = account_truth._store_account_deals(
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
    """Regression for the production text/varchar AmbiguousParameter failure."""
    harness = _LedgerHarness()
    now = datetime.now(UTC)
    account_truth._mark_full_backfill(
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
