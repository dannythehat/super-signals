from pathlib import Path


def test_member_telegram_format_is_spacious_and_status_driven() -> None:
    source = Path("services/api/app/telegram_publisher_canonical.py").read_text()
    assert "TRADE UPDATE 📈 · {tp_label} HIT" in source
    assert "<b>Today’s P&L</b>" in source
    assert "<b>Trade Status</b>" in source
    assert "WON 🥳" in source
    assert "CLOSED IN PROFIT 🥳" in source
    assert "PENDING ⏳" in source
    assert "Trade complete" in source
    assert "identity.marker" not in source


def test_member_provider_identity_uses_named_emojis_not_colour_pairs() -> None:
    source = Path("services/api/app/telegram_trade_ledger.py").read_text()
    assert '"🇯🇵"' in source
    assert '"🏎️"' in source
    assert "_PROVIDER_COLOURS" not in source


def test_today_pnl_is_account_value_change_not_realised_only() -> None:
    source = Path("services/api/app/telegram_trade_ledger.py").read_text()
    assert "account_value - today_opening_value" in source
    public = Path("services/api/app/routes/gold_quote.py").read_text()
    assert "account_value_days" in public
    assert "opening_balance" in public
    assert "closing_balance" in public


def test_provider_tp_milestones_drive_member_target_status() -> None:
    source = Path("services/api/app/telegram_trade_ledger.py").read_text()
    assert "provider_hits" in source
    assert "provider_reported_hit" in source
    assert "milestone_pattern" in source


def test_minimum_lot_partial_failure_cannot_force_flatten_trade() -> None:
    source = Path("services/api/app/management_reliability_runtime.py").read_text()
    assert "partial_volume_below_broker_minimum" in source
