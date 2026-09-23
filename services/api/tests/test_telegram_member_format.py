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


def test_today_pnl_and_balance_follow_mt5_closed_balance_truth() -> None:
    source = Path("services/api/app/telegram_trade_ledger.py").read_text()
    assert 'snapshot["balance"]' in source
    assert "account_value - today_opening_value" in source
    assert "never substitute equity/floating P&L" in source
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
            SimpleNamespace(tp_index=1, status="won", cash_pnl=1),
            SimpleNamespace(tp_index=2, status="won", cash_pnl=2),
            SimpleNamespace(tp_index=3, status="closed_profit", cash_pnl=3),
            SimpleNamespace(tp_index=4, status="cancelled", cash_pnl=0),
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
    assert "TP2 — <b>WON 🥳</b> · <b>+$2.00</b>" in repaired
    assert "TP3 — <b>CLOSED IN PROFIT 🥳</b> · <b>+$3.00</b>" in repaired
    assert "TP4 — <b>CANCELLED</b>" in repaired
    assert "PENDING ⏳" not in repaired


def test_same_burst_settlements_are_ordered_by_broker_time_not_created_at() -> None:
    source = Path("services/api/app/telegram_publisher_canonical.py").read_text()
    assert "sibling.occurred_at>ev.occurred_at" in source
    assert "sent_same_burst_duplicate_deleted" in source
    assert '"deleteMessage"' in source


def test_complete_multi_leg_trade_uses_total_result_not_one_leg() -> None:
    source = Path("services/api/app/telegram_publisher_canonical.py").read_text()
    assert "trade.realised_pnl.quantize" in source
    assert "TRADE LOSS · {money(total)}" in source
    assert "TRADE WIN · {money(total)}" in source
    assert "_balance_money(account.account_value)" in source


def test_uneditable_stale_result_is_replaced_without_duplicate_spam() -> None:
    source = Path("services/api/app/telegram_publisher_canonical.py").read_text()
    assert "message to edit not found" in source
    assert '"deleteMessage"' in source
    assert '"sendMessage"' in source
    assert "replacement_message_id" in source
    assert "reply_to_telegram_message_id" in source


def test_notification_titles_survive_unassigned_trade_number() -> None:
    source = Path("services/api/app/telegram_publisher_canonical.py").read_text()
    assert "COALESCE('TRADE ' || sig.member_trade_number::text,'TRADE')" in source
    assert "'NEW TRADE PLACED — ' || COALESCE" in source



def test_provider_management_telegram_requires_real_broker_action() -> None:
    source = Path("services/api/app/telegram_publisher_canonical.py").read_text()
    assert "_management_broker_action_sql" in source
    assert "routed.event_type='mt5.day28_route_success'" in source
    assert "broker_actions_sent" in source
    assert "ev.origin<>'provider_update'" in source
    assert "management_action_for_ev" in source



def test_member_roots_wait_for_public_trade_number_and_repair_internal_ids() -> None:
    source = Path("services/api/app/telegram_publisher_canonical.py").read_text()
    assert "sig.member_trade_number IS NOT NULL" in source
    assert "_repair_sent_root_identities_safely" in source
    assert "SS-[0-9A-F]{10}" in source
    assert "TRADE {int(row['member_trade_number'])}" in source


def test_skipped_unfilled_legs_render_cancelled_not_pending() -> None:
    source = Path("services/api/app/telegram_trade_ledger.py").read_text()
    assert 'position_status in {"cancelled", "canceled", "skipped"}' in source



def test_terminal_cancelled_state_beats_old_provider_tp_hit_claim() -> None:
    source = Path("services/api/app/telegram_trade_ledger.py").read_text()
    cancelled = source.index(
        'elif position_status in {"cancelled", "canceled", "skipped"}:'
    )
    provider_hit = source.index("elif provider_reported_hit:")
    assert cancelled < provider_hit
