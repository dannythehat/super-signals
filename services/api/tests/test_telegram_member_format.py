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
    assert "_reserve_financial_publication" in source
    assert "_financial_lines(rolling_balance, rolling_daily)" in source


def test_live_board_and_trade_posts_use_exact_website_figures() -> None:
    source = Path("services/api/app/telegram_publisher_canonical.py").read_text()
    assert "from app.running_daily_balance import account_value_days" in source
    assert "def _website_financial_snapshot" in source
    assert "current_day.closing_value" in source
    assert "current_day.pnl" in source
    assert "(prior_balance + delta)" not in source
    board = source[source.index("    def _render_live_board"):source.index("__all__")]
    assert "_website_financial_snapshot(session)" in board


def test_member_provider_identity_uses_named_emojis_not_colour_pairs() -> None:
    source = Path("services/api/app/telegram_trade_ledger.py").read_text()
    assert '"🇯🇵"' in source
    assert '"🏎️"' in source
    assert "_PROVIDER_COLOURS" not in source


def test_today_pnl_and_balance_follow_full_vantage_equity_from_9pm() -> None:
    source = Path("services/api/app/telegram_trade_ledger.py").read_text()
    assert 'snapshot["equity"]' in source
    assert "account_value - today_opening_value" in source
    assert "hour=21" in source
    assert "trading_day_start" in source
    assert "21:00 Sofia" in source
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


def test_lifecycle_posts_use_immutable_event_time_status() -> None:
    source = Path("services/api/app/telegram_publisher_canonical.py").read_text()
    assert "_trade_status_lines_as_of" in source
    assert "closed_at > event_at" in source
    assert "_replace_trade_status_snapshot" not in source


def test_realised_settlements_are_kept_in_broker_time_order() -> None:
    source = Path("services/api/app/telegram_publisher_canonical.py").read_text()
    assert "ORDER BY ev.occurred_at,ev.created_at,pub.id" in source
    assert "same_burst_settlement_collapsed" not in source
    assert "sent_same_burst_duplicate_deleted" not in source


def test_current_day_broker_settlements_recover_but_history_never_replays() -> None:
    source = Path("services/api/app/telegram_publisher_canonical.py").read_text()
    assert "_CURRENT_SOFIA_DAY_START_SQL" in source
    assert "ev.event_type<>'broker_position_settled'" in source
    assert "ev.occurred_at >= (date_trunc('day', timezone('Europe/Sofia', now())) AT TIME ZONE 'Europe/Sofia')" in source
    assert "historical_replay_deleted" in source


def test_broker_settlement_can_publish_without_sent_root() -> None:
    source = Path("services/api/app/telegram_publisher_canonical.py").read_text()
    assert "LEFT JOIN telegram_publications AS root" in source
    assert "ev.event_type='broker_position_settled'" in source
    assert "_any_confirmed_placement_sql" in source
    assert "any_placement_for_ev" in source
    assert "attempt.reply_to_message_id is not None" in source


def test_aggregate_broker_results_are_audit_only() -> None:
    source = Path("services/api/app/telegram_publisher_canonical.py").read_text()
    assert "aggregate_broker_result_audit_only" in source
    assert "ev.event_type NOT LIKE 'broker_result_%'" in source


def test_later_tp_waits_for_missing_earlier_settlement_event() -> None:
    source = Path("services/api/app/telegram_publisher_canonical.py").read_text()
    assert "earlier.closed_at<ev.occurred_at" in source
    assert "earlier_ev.event_type='broker_position_settled'" in source


def test_complete_multi_leg_trade_uses_total_result_not_one_leg() -> None:
    source = Path("services/api/app/telegram_publisher_canonical.py").read_text()
    assert "trade.realised_pnl.quantize" in source
    assert "TRADE LOSS · {money(total)}" in source
    assert "TRADE WIN · {money(total)}" in source
    assert "_financial_lines(rolling_balance, rolling_daily)" in source


def test_uneditable_lifecycle_result_is_never_reposted() -> None:
    source = Path("services/api/app/telegram_publisher_canonical.py").read_text()
    assert "message to edit not found" in source
    assert "sent_lifecycle_uneditable_no_replacement" in source
    assert "Historical Telegram update is uneditable; no replacement was sent." in source


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
