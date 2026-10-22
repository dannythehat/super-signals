"""Ensure owner-approved October 2026 basket activation is exact and forward-only."""

from __future__ import annotations

import ast
from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations/versions/0128_activate_oct_profitable.py"
)
SOURCE = MIGRATION.read_text(encoding="utf-8")


def test_migration_has_single_linear_parent() -> None:
    assert 'revision: str = "0128_oct_positive"' in SOURCE
    assert 'down_revision: str | None = "0127_profit_only_demo"' in SOURCE


def test_only_two_shadow_providers_promoted_no_other_eligibility_changes() -> None:
    tree = ast.parse(SOURCE)
    providers = next(
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "_SHADOW_TO_TESTING"
            for target in node.targets
        )
    )
    assert ast.literal_eval(providers) == (
        ("4b214aaf-dc12-4977-85b1-d269d17716f3", -1001391285381, "GOLD TRADING PK"),
        ("13af029e-5491-4bba-80e8-13c74b360d31", -1002144761347, "TOP 1% TRADES 🥇"),
    )


def test_activation_requires_verified_october_and_alltime_cash_profit() -> None:
    assert "performance_trade_outcomes" in SOURCE
    assert "o.broker_deal_count>0" in SOURCE
    assert "o.cash_pnl" in SOURCE
    assert "2026-10-01" in SOURCE
    assert "2026-11-01" in SOURCE
    assert 'Decimal(str(row["all_time_pnl"])) <= 0' in SOURCE
    assert 'Decimal(str(row["october_pnl"])) <= 0' in SOURCE


def test_no_change_to_money_execution_live_graduation_or_historic_signals() -> None:
    assert "UPDATE sources SET status='testing',updated_at=now()" in SOURCE
    assert "AND status='shadow'" in SOURCE
    assert "if row[\"status\"] == \"testing\"" in SOURCE
    assert "ON CONFLICT(source_id) DO NOTHING" in SOURCE
    assert "if bool(row[\"graduated\"])" in SOURCE
    assert '"historical_replay_enabled": False' in SOURCE
    assert '"live_member_distribution_enabled": False' in SOURCE
    assert "UPDATE positions" not in SOURCE
    assert "UPDATE user_trading_controls" not in SOURCE
