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


def test_sent_trade_status_repair_edits_in_place_without_new_post() -> None:
    source = Path("services/api/app/telegram_publisher_canonical.py").read_text()
    assert '"editMessageText"' in source
    assert "Telegram trade message corrected in place" in source
    assert "_repair_sent_trade_messages_safely" in source
    assert "Book partial profit at TP1" in source
    assert "TP{target} hit. Book partial profit." in source


def test_settlement_formatter_uses_resolved_tp_index_not_missing_row_field() -> None:
    source = Path("services/api/app/telegram_publisher_canonical.py").read_text()
    assert 'if leg.tp_index == int(tp_index or 1)' in source
    assert 'row["tp_index"]' not in source


def test_trade_status_snapshot_repair_replaces_stale_pending_lines() -> None:
    from types import SimpleNamespace
    from app.telegram_publisher_canonical import CanonicalTelegramPublisherManager

    trade = SimpleNamespace(
        legs=[
            SimpleNamespace(tp_index=1, status="won"),
            SimpleNamespace(tp_index=2, status="won"),
            SimpleNamespace(tp_index=3, status="closed_profit"),
            SimpleNamespace(tp_index=4, status="cancelled"),
        ]
    )
    old = (
        "🇯🇵 <b>TIG’s Asia Trades</b>\n\n"
        "<b>TRADE UPDATE 📈 · TP3 HIT</b>\n\n"
        "<b>Trade Status</b>\n\n"
        "TP1 — <b>WON 🥳</b>\n"
        "TP2 — <b>PENDING ⏳</b>\n"
        "TP3 — <b>WON 🥳</b>\n"
        "TP4 — <b>PENDING ⏳</b>"
    )
    repaired = CanonicalTelegramPublisherManager._replace_trade_status_snapshot(old, trade)
    assert "TP2 — <b>WON 🥳</b>" in repaired
    assert "TP3 — <b>CLOSED IN PROFIT 🥳</b>" in repaired
    assert "TP4 — <b>CANCELLED</b>" in repaired
    assert "PENDING ⏳" not in repaired
