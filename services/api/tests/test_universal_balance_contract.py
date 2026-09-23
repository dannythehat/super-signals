"""One balance, owned by the broker, on every surface.

Six mutually inconsistent balances were reachable at once: the broker's closed-trade
balance field, a hard-coded 1517.23 paper baseline dated 31 Aug, that baseline plus
realised P&L, the dashboard calendar compounding forward from it, reviewed-provider cash
with no broker deal behind it, and the Vantage account value the owner actually reads.

The owner's ruling is that the Vantage account value is the balance, universally and
permanently. These tests pin that so no surface can quietly reintroduce a second
definition.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from app.trading_accounting import CanonicalTradingAccountingService

APP = Path(__file__).resolve().parents[1] / "app"
OWNER = UUID("ea604df2-f8ee-47d1-bc51-f0078dbf160d")


def _strip_docstrings(node: ast.AST) -> ast.AST:
    for item in ast.walk(node):
        if not isinstance(
            item, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            continue
        body = getattr(item, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            item.body = body[1:] or [ast.Pass()]
    return node


def _code(name: str) -> str:
    """Executable code only: comments and docstrings removed.

    A contract that forbade *mentioning* a name would also forbid explaining why it was
    removed, so these tests read what the module actually does.
    """
    tree = _strip_docstrings(ast.parse((APP / name).read_text()))
    return ast.unparse(tree)


def _function_code(name: str, function: str) -> str:
    tree = ast.parse((APP / name).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function:
            return ast.unparse(_strip_docstrings(node))
    raise AssertionError(f"{function} not found in {name}")


def _service() -> CanonicalTradingAccountingService:
    def _unusable_session_factory():  # noqa: ANN202
        raise AssertionError(
            "the balance must not require a database read; the broker already gave it"
        )

    return CanonicalTradingAccountingService(_unusable_session_factory)  # type: ignore[arg-type]


def test_displayed_balance_returns_the_broker_account_value_unmodified() -> None:
    """2050.64 in, 2050.64 out. No baseline, no carry-in, no realised-P&L derivation."""
    service = _service()
    for value in ("2050.64", "1364.87", "1000.00", "0.00"):
        assert service.displayed_balance(
            OWNER, broker_account_value=Decimal(value)
        ) == Decimal(value)


def test_displayed_balance_is_identical_for_every_account() -> None:
    """The Owner demo account had its own derivation. It no longer does."""
    service = _service()
    owner = service.displayed_balance(OWNER, broker_account_value=Decimal("2050.64"))
    member = service.displayed_balance(uuid4(), broker_account_value=Decimal("2050.64"))
    assert owner == member == Decimal("2050.64")


def test_no_hard_coded_baseline_survives_in_any_balance_path() -> None:
    """1517.23 was a value the account never held, on a date the owner never started
    from. It must not reappear as a balance anywhere."""
    accounting = _code("trading_accounting.py")
    assert "OWNER_DEMO_BASELINE_BALANCE" not in accounting
    assert "PAPER_RUN_BASELINE_BALANCE" not in accounting
    assert "1517.23" not in accounting


def test_displayed_balance_adds_no_computed_cash() -> None:
    """The failure mode was a balance built by adding things to a baseline. The body is
    pinned to reject that shape rather than trusting review to catch it."""
    body = _function_code("trading_accounting.py", "displayed_balance")
    for forbidden in ("all_time_pnl", "realised_between", "+ self.", "BASELINE"):
        assert forbidden not in body, f"displayed_balance must not reference {forbidden}"


def test_every_surface_passes_the_account_value_not_the_balance_field() -> None:
    """The broker's `balance` field excludes floating P&L, so it is not what Vantage
    shows. Each caller must hand over the account value."""
    callers = (
        "mt5_execution_day26.py",
        "dashboard_resilient_runtime.py",
        "execution_capture_reliability.py",
        "routes/gold_quote.py",
        "routes/dashboard_day32.py",
    )
    for name in callers:
        source = _code(name)
        assert "broker_account_value=" in source, f"{name} does not pass an account value"
        assert "broker_balance=" not in source, f"{name} still passes a balance field"


def test_execution_sizes_from_the_account_value() -> None:
    """1% of the company paper balance, always - which is the Vantage account value."""
    source = _code("mt5_execution_day26.py")
    assert "broker_account_value=live_state.account.equity" in source
    assert "risk_balance = live_state.account.balance" not in source


def test_telegram_publishes_the_same_balance_it_sizes_from() -> None:
    """The published '1% = $X' and the published account value must come from one
    number, or members read a figure the account never used."""
    # ast.unparse normalises string quotes, so the expectation uses single quotes.
    source = _code("telegram_trade_ledger.py")
    assert "account_value * Decimal('0.01')" in source
    assert "mt5_balance * Decimal('0.01')" not in source


def test_calendar_reconstructs_backwards_from_the_account_value() -> None:
    """The Owner calendar used to compound forward from the baseline, so it drifted from
    Vantage by construction. It now walks back from the real account value through the
    broker's own balance-changing deals."""
    body = _function_code("trading_accounting.py", "daily")
    assert "_daily_total_balance_changes" in body
    assert "running_end = _money(Decimal(str(broker_account_value)))" in body
    assert "uses_synthetic_demo_balance" not in body


def test_capital_movements_stay_inside_the_reconstructed_ledger() -> None:
    """Four DEAL_TYPE_BALANCE corrections on the Owner account are real results the owner
    recovered by hand after MetaAPI failed to place the trades. The calendar must keep
    reading the deal series that contains them, not trade exits alone."""
    body = _function_code("trading_accounting.py", "_daily_total_balance_changes")
    assert "entry_type" not in body, (
        "filtering _daily_total_balance_changes by entry_type would drop the "
        "DEAL_TYPE_BALANCE corrections out of the ledger"
    )
