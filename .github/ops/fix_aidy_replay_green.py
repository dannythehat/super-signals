from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"Expected one match in {path}, found {count}: {old[:100]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


# Restore the existing Provider Lab non-blocking DB contract. The public/legacy path
# remains available only for mixed/unknown rows, but it must never block the event loop.
replace_once(
    "services/api/app/shadow_trading_v4.py",
    '''    def _evaluate_public_rows_sync(self, rows, bid: Decimal, ask: Decimal, quote_mode: str) -> int:\n        changed = 0\n        with self._session_factory() as session:\n            for row in rows:\n                changed += int(\n                    self._evaluate_row(\n                        session,\n                        row,\n                        bid=bid,\n                        ask=ask,\n                        quote_mode=quote_mode,\n                    )\n                )\n            session.commit()\n        return changed\n\n\n__all__ = ["ShadowTradeManager"]\n''',
    '''    def _evaluate_public_rows_sync(self, rows, bid: Decimal, ask: Decimal, quote_mode: str) -> int:\n        changed = 0\n        with self._session_factory() as session:\n            for row in rows:\n                changed += int(\n                    self._evaluate_row(\n                        session,\n                        row,\n                        bid=bid,\n                        ask=ask,\n                        quote_mode=quote_mode,\n                    )\n                )\n            session.commit()\n        return changed\n\n    async def _evaluate_all(self, *, bid: Decimal, ask: Decimal, quote_mode: str) -> int:\n        \"\"\"Keep the inherited fair evaluator's synchronous DB work off the event loop.\"\"\"\n        async with self._evaluation_lock:\n            rows = await asyncio.to_thread(self._active_rows)\n            if not rows:\n                return 0\n            return await asyncio.to_thread(\n                self._evaluate_public_rows_sync,\n                rows,\n                bid,\n                ask,\n                quote_mode,\n            )\n\n\n__all__ = ["ShadowTradeManager"]\n''',
)

# Migration 0055 has never been deployed, so correct it in place: the lifecycle
# watermark is an explicit applied chronological prefix, independent of the M1 cursor.
replace_once(
    "services/api/migrations/versions/0055_aidy_provider_lab_market_truth.py",
    '''    op.add_column("shadow_trades", sa.Column("aidy_lifecycle_watermark", sa.String(length=64), nullable=True))\n    op.add_column("shadow_trades", sa.Column("aidy_terminal", sa.Boolean(), nullable=False, server_default=sa.false()))\n''',
    '''    op.add_column("shadow_trades", sa.Column("aidy_lifecycle_watermark", sa.String(length=64), nullable=True))\n    op.add_column("shadow_trades", sa.Column("aidy_lifecycle_applied_count", sa.Integer(), nullable=False, server_default="0"))\n    op.add_column("shadow_trades", sa.Column("aidy_terminal", sa.Boolean(), nullable=False, server_default=sa.false()))\n''',
)
replace_once(
    "services/api/migrations/versions/0055_aidy_provider_lab_market_truth.py",
    '''            aidy_lifecycle_watermark=NULL,\n            aidy_state_version=0,\n''',
    '''            aidy_lifecycle_watermark=NULL,\n            aidy_lifecycle_applied_count=0,\n            aidy_state_version=0,\n''',
)
replace_once(
    "services/api/migrations/versions/0055_aidy_provider_lab_market_truth.py",
    '''    op.create_check_constraint(\n        "ck_shadow_aidy_state_version",\n        "shadow_trades",\n        "aidy_state_version >= 0",\n    )\n''',
    '''    op.create_check_constraint(\n        "ck_shadow_aidy_state_version",\n        "shadow_trades",\n        "aidy_state_version >= 0",\n    )\n    op.create_check_constraint(\n        "ck_shadow_aidy_lifecycle_count",\n        "shadow_trades",\n        "aidy_lifecycle_applied_count >= 0",\n    )\n''',
)
replace_once(
    "services/api/migrations/versions/0055_aidy_provider_lab_market_truth.py",
    '''    op.drop_constraint("ck_shadow_aidy_state_version", "shadow_trades", type_="check")\n    op.drop_constraint("ck_shadow_aidy_score_mode", "shadow_trades", type_="check")\n''',
    '''    op.drop_constraint("ck_shadow_aidy_lifecycle_count", "shadow_trades", type_="check")\n    op.drop_constraint("ck_shadow_aidy_state_version", "shadow_trades", type_="check")\n    op.drop_constraint("ck_shadow_aidy_score_mode", "shadow_trades", type_="check")\n''',
)
replace_once(
    "services/api/migrations/versions/0055_aidy_provider_lab_market_truth.py",
    '''        "aidy_lifecycle_watermark",\n        "aidy_state_version",\n''',
    '''        "aidy_lifecycle_applied_count",\n        "aidy_lifecycle_watermark",\n        "aidy_state_version",\n''',
)

resolver = "services/api/app/aidy_shadow_resolver.py"
# Prefix ordering is the exact deterministic replay ordering. A late event inserted
# earlier chronologically therefore invalidates the already-applied prefix digest.
replace_once(
    resolver,
    '''        key=lambda item: (item["created_at"], item["id"]),\n''',
    '''        key=lambda item: (item["occurred_at"], item["created_at"], item["id"]),\n''',
)
replace_once(
    resolver,
    '''    lifecycle_watermark: str | None\n    score_block_reason: str | None = None\n''',
    '''    lifecycle_watermark: str | None\n    lifecycle_applied_count: int = 0\n    score_block_reason: str | None = None\n''',
)
replace_once(
    resolver,
    '''        lifecycle_watermark=lifecycle_mark,\n    )\n''',
    '''        lifecycle_watermark=lifecycle_mark,\n        lifecycle_applied_count=0,\n    )\n''',
)
# Remove the old market-cursor-derived event skipping entirely.
replace_once(
    resolver,
    '''    event_index = 0\n    if not full_replay and state.market_cursor is not None:\n        already_through = _utc(state.market_cursor) + timedelta(minutes=1)\n        while (\n            event_index < len(ordered_events)\n            and _utc(ordered_events[event_index]["occurred_at"]) < already_through\n        ):\n            event_index += 1\n\n    signal_minute = _minute_floor(signal_posted_at)\n''',
    '''    event_index = 0\n    # `events` contains exactly the unapplied chronological lifecycle suffix for an\n    # incremental replay, or the complete lifecycle sequence for a full replay. Event\n    # consumption is tracked only by lifecycle_applied_count; the market cursor is never\n    # used to infer whether a provider instruction was consumed.\n\n    signal_minute = _minute_floor(signal_posted_at)\n''',
)
# Consume pre-bar events explicitly, including terminal actions.
replace_once(
    resolver,
    '''        while (\n            event_index < len(ordered_events)\n            and _utc(ordered_events[event_index]["occurred_at"]) <= bar_open\n        ):\n            if not _apply_actions(\n                state,\n                geometry=geometry,\n                event=ordered_events[event_index],\n                sibling_entries=sibling_entries,\n            ):\n                if state.market_cursor is None:\n                    state.market_cursor = bar_open\n                return state\n            event_index += 1\n''',
    '''        while (\n            event_index < len(ordered_events)\n            and _utc(ordered_events[event_index]["occurred_at"]) <= bar_open\n        ):\n            keep_going = _apply_actions(\n                state,\n                geometry=geometry,\n                event=ordered_events[event_index],\n                sibling_entries=sibling_entries,\n            )\n            state.lifecycle_applied_count += 1\n            event_index += 1\n            if not keep_going:\n                if state.market_cursor is None:\n                    state.market_cursor = bar_open\n                return state\n''',
)
# Partial-signal-minute in-bar event consumption.
replace_once(
    resolver,
    '''                if _event_price_active(\n                    state,\n                    geometry=geometry,\n                    event=event,\n                    bar=bar,\n                    sibling_entries=sibling_entries,\n                ):\n                    _block(\n                        state,\n                        reason="aidy_m1_management_bar_ambiguous",\n                        when=bar_end,\n                        note="management_inside_partial_signal_minute",\n                        bar=bar,\n                    )\n                    state.market_cursor = bar_open\n                    return state\n                if not _apply_actions(\n                    state,\n                    geometry=geometry,\n                    event=event,\n                    sibling_entries=sibling_entries,\n                ):\n                    state.market_cursor = bar_open\n                    return state\n                event_index += 1\n''',
    '''                if _event_price_active(\n                    state,\n                    geometry=geometry,\n                    event=event,\n                    bar=bar,\n                    sibling_entries=sibling_entries,\n                ):\n                    _block(\n                        state,\n                        reason="aidy_m1_management_bar_ambiguous",\n                        when=bar_end,\n                        note="management_inside_partial_signal_minute",\n                        bar=bar,\n                    )\n                    state.lifecycle_applied_count += 1\n                    event_index += 1\n                    state.market_cursor = bar_open\n                    return state\n                keep_going = _apply_actions(\n                    state,\n                    geometry=geometry,\n                    event=event,\n                    sibling_entries=sibling_entries,\n                )\n                state.lifecycle_applied_count += 1\n                event_index += 1\n                if not keep_going:\n                    state.market_cursor = bar_open\n                    return state\n''',
)
# Normal in-bar ambiguity consumes the provider event as the deterministic reason for block.
replace_once(
    resolver,
    '''        for event in in_bar_events:\n            if _event_price_active(\n                state,\n                geometry=geometry,\n                event=event,\n                bar=bar,\n                sibling_entries=sibling_entries,\n            ):\n                _block(\n                    state,\n                    reason="aidy_m1_management_bar_ambiguous",\n                    when=bar_end,\n                    note="management_instruction_inside_price_active_m1_bar",\n                    bar=bar,\n                )\n                state.market_cursor = bar_open\n                return state\n''',
    '''        for event in in_bar_events:\n            if _event_price_active(\n                state,\n                geometry=geometry,\n                event=event,\n                bar=bar,\n                sibling_entries=sibling_entries,\n            ):\n                _block(\n                    state,\n                    reason="aidy_m1_management_bar_ambiguous",\n                    when=bar_end,\n                    note="management_instruction_inside_price_active_m1_bar",\n                    bar=bar,\n                )\n                state.lifecycle_applied_count += 1\n                event_index += 1\n                state.market_cursor = bar_open\n                return state\n''',
)
# Normal post-price action application consumes exactly one event independent of market cursor.
replace_once(
    resolver,
    '''        for event in in_bar_events:\n            if not _apply_actions(\n                state,\n                geometry=geometry,\n                event=event,\n                sibling_entries=sibling_entries,\n            ):\n                state.market_cursor = bar_open\n                return state\n            event_index += 1\n''',
    '''        for event in in_bar_events:\n            keep_going = _apply_actions(\n                state,\n                geometry=geometry,\n                event=event,\n                sibling_entries=sibling_entries,\n            )\n            state.lifecycle_applied_count += 1\n            event_index += 1\n            if not keep_going:\n                state.market_cursor = bar_open\n                return state\n''',
)
# Persisted state carries its explicit lifecycle prefix count and digest; loaded full-set
# hash is no longer injected as though it had already been applied.
replace_once(
    resolver,
    '''    def _state_from_persisted(\n        self,\n        trade: dict[str, Any],\n        legs: list[LegState],\n        mark: str,\n    ) -> ResolutionState:\n''',
    '''    def _state_from_persisted(\n        self,\n        trade: dict[str, Any],\n        legs: list[LegState],\n    ) -> ResolutionState:\n''',
)
replace_once(
    resolver,
    '''            market_cursor=trade.get("aidy_m1_cursor_at"),\n            lifecycle_watermark=mark,\n            score_block_reason=(\n''',
    '''            market_cursor=trade.get("aidy_m1_cursor_at"),\n            lifecycle_watermark=trade.get("aidy_lifecycle_watermark"),\n            lifecycle_applied_count=int(trade.get("aidy_lifecycle_applied_count") or 0),\n            score_block_reason=(\n''',
)
# CAS now includes the independent applied prefix count in addition to version/cursor/digest.
replace_once(
    resolver,
    '''        expected_cursor: datetime | None,\n        expected_lifecycle_watermark: str | None,\n        loaded_lifecycle_watermark: str,\n    ) -> None:\n''',
    '''        expected_cursor: datetime | None,\n        expected_lifecycle_watermark: str | None,\n        expected_lifecycle_count: int,\n        loaded_lifecycle_watermark: str,\n    ) -> None:\n''',
)
replace_once(
    resolver,
    '''                        aidy_resolution_note=:note,aidy_m1_cursor_at=:cursor,\n                        aidy_lifecycle_watermark=:new_lifecycle_mark,\n                        aidy_state_version=aidy_state_version+1,\n''',
    '''                        aidy_resolution_note=:note,aidy_m1_cursor_at=:cursor,\n                        aidy_lifecycle_watermark=:new_lifecycle_mark,\n                        aidy_lifecycle_applied_count=:new_lifecycle_count,\n                        aidy_state_version=aidy_state_version+1,\n''',
)
replace_once(
    resolver,
    '''                      AND aidy_m1_cursor_at IS NOT DISTINCT FROM :expected_cursor\n                      AND aidy_lifecycle_watermark IS NOT DISTINCT FROM :expected_lifecycle_mark\n                      AND NOT COALESCE(aidy_terminal,false)\n''',
    '''                      AND aidy_m1_cursor_at IS NOT DISTINCT FROM :expected_cursor\n                      AND aidy_lifecycle_watermark IS NOT DISTINCT FROM :expected_lifecycle_mark\n                      AND aidy_lifecycle_applied_count=:expected_lifecycle_count\n                      AND NOT COALESCE(aidy_terminal,false)\n''',
)
replace_once(
    resolver,
    '''                    "new_lifecycle_mark": loaded_lifecycle_watermark,\n                    "terminal": state.terminal,\n''',
    '''                    "new_lifecycle_mark": state.lifecycle_watermark,\n                    "new_lifecycle_count": state.lifecycle_applied_count,\n                    "terminal": state.terminal,\n''',
)
replace_once(
    resolver,
    '''                    "expected_lifecycle_mark": expected_lifecycle_watermark,\n                },\n''',
    '''                    "expected_lifecycle_mark": expected_lifecycle_watermark,\n                    "expected_lifecycle_count": expected_lifecycle_count,\n                },\n''',
)
# Resolve lifecycle suffix from the explicit prefix. If the prefix no longer matches or
# a newly appended event belongs behind the market cursor, force a full replay.
replace_once(
    resolver,
    '''        expected_version = int(trade.get("aidy_state_version") or 0)\n        expected_cursor = trade.get("aidy_m1_cursor_at")\n        expected_mark = trade.get("aidy_lifecycle_watermark")\n        revalidation = trade.get("score_exclusion_reason") in {\n            "market_data_not_observed",\n            "aidy_m1_revalidation_required",\n        }\n        lifecycle_changed = expected_mark != loaded_mark\n        full_replay = revalidation or expected_cursor is None or lifecycle_changed\n        if full_replay:\n''',
    '''        expected_version = int(trade.get("aidy_state_version") or 0)\n        expected_cursor = trade.get("aidy_m1_cursor_at")\n        expected_mark = trade.get("aidy_lifecycle_watermark")\n        expected_lifecycle_count = int(trade.get("aidy_lifecycle_applied_count") or 0)\n        ordered_events = sorted(\n            events,\n            key=lambda item: (\n                _utc(item["occurred_at"]),\n                _utc(item["created_at"]),\n                str(item["id"]),\n            ),\n        )\n        prefix_valid = expected_lifecycle_count <= len(ordered_events)\n        if prefix_valid and expected_lifecycle_count:\n            prefix_valid = (\n                lifecycle_watermark(ordered_events[:expected_lifecycle_count]) == expected_mark\n            )\n        elif prefix_valid and expected_lifecycle_count == 0:\n            prefix_valid = expected_mark in {None, lifecycle_watermark([])}\n        pending_events = (\n            ordered_events[expected_lifecycle_count:] if prefix_valid else ordered_events\n        )\n        cursor_boundary = (\n            _utc(expected_cursor) + timedelta(minutes=1)\n            if isinstance(expected_cursor, datetime)\n            else None\n        )\n        late_event_behind_market = bool(\n            cursor_boundary is not None\n            and any(_utc(item["occurred_at"]) < cursor_boundary for item in pending_events)\n        )\n        revalidation = trade.get("score_exclusion_reason") in {\n            "market_data_not_observed",\n            "aidy_m1_revalidation_required",\n        }\n        full_replay = (\n            revalidation\n            or expected_cursor is None\n            or not prefix_valid\n            or late_event_behind_market\n        )\n        if full_replay:\n''',
)
replace_once(
    resolver,
    '''            state = _initial_state(\n                geometry=geometry,\n                leg_ids=leg_ids,\n                signal_posted_at=posted_at,\n                lifecycle_mark=loaded_mark,\n            )\n            start = _minute_floor(posted_at)\n        else:\n            state = self._state_from_persisted(trade, legs, loaded_mark)\n            assert expected_cursor is not None\n            start = _utc(expected_cursor)\n''',
    '''            state = _initial_state(\n                geometry=geometry,\n                leg_ids=leg_ids,\n                signal_posted_at=posted_at,\n                lifecycle_mark=lifecycle_watermark([]),\n            )\n            events_to_replay = ordered_events\n            start = _minute_floor(posted_at)\n        else:\n            state = self._state_from_persisted(trade, legs)\n            events_to_replay = pending_events\n            assert expected_cursor is not None\n            start = _utc(expected_cursor)\n''',
)
replace_once(
    resolver,
    '''            geometry=geometry,\n            events=events,\n            bars=usable,\n''',
    '''            geometry=geometry,\n            events=events_to_replay,\n            bars=usable,\n''',
)
replace_once(
    resolver,
    '''        state.lifecycle_watermark = loaded_mark\n        await asyncio.to_thread(\n''',
    '''        if state.lifecycle_applied_count > len(ordered_events):\n            raise ValueError("aidy_lifecycle_applied_prefix_invalid")\n        state.lifecycle_watermark = lifecycle_watermark(\n            ordered_events[:state.lifecycle_applied_count]\n        )\n        await asyncio.to_thread(\n''',
)
replace_once(
    resolver,
    '''            expected_cursor=expected_cursor,\n            expected_lifecycle_watermark=expected_mark,\n            loaded_lifecycle_watermark=loaded_mark,\n''',
    '''            expected_cursor=expected_cursor,\n            expected_lifecycle_watermark=expected_mark,\n            expected_lifecycle_count=expected_lifecycle_count,\n            loaded_lifecycle_watermark=loaded_mark,\n''',
)

# Strengthen the adversarial suite: lifecycle consumption is now explicitly independent
# of M1 and the legacy evaluator regression remains covered by its original test.
tests = "services/api/tests/test_aidy_provider_lab_market_truth.py"
replace_once(
    tests,
    '''    assert "aidy_original_geometry" in resolver\n    assert "aidy_lifecycle_watermark" in resolver and "aidy_m1_cursor_at" in resolver\n    assert "aidy_state_version=:expected_version" in resolver\n''',
    '''    assert "aidy_original_geometry" in resolver\n    assert "aidy_lifecycle_watermark" in resolver and "aidy_m1_cursor_at" in resolver\n    assert "aidy_lifecycle_applied_count" in resolver\n    assert "already_through = _utc(state.market_cursor)" not in resolver\n    assert "aidy_state_version=:expected_version" in resolver\n''',
)
replace_once(
    tests,
    '''    assert "aidy_lifecycle_watermark IS NOT DISTINCT FROM :expected_lifecycle_mark" in resolver\n    assert "AND NOT COALESCE(aidy_terminal,false)" in resolver\n''',
    '''    assert "aidy_lifecycle_watermark IS NOT DISTINCT FROM :expected_lifecycle_mark" in resolver\n    assert "aidy_lifecycle_applied_count=:expected_lifecycle_count" in resolver\n    assert "AND NOT COALESCE(aidy_terminal,false)" in resolver\n''',
)
replace_once(
    tests,
    '''def test_7_lifecycle_watermark_detects_late_old_event() -> None:\n    late = _event(1, {"type": "move_to_break_even", "target": "all", "value": None}, key="late")\n    first = _event(3, {"type": "move_to_break_even", "target": "all", "value": None}, key="first")\n    late["created_at"] = BASE + timedelta(minutes=4)\n    assert lifecycle_watermark([first]) != lifecycle_watermark([first, late])\n\n\n''',
    '''def test_7_lifecycle_watermark_detects_late_old_event() -> None:\n    late = _event(1, {"type": "move_to_break_even", "target": "all", "value": None}, key="late")\n    first = _event(3, {"type": "move_to_break_even", "target": "all", "value": None}, key="first")\n    late["created_at"] = BASE + timedelta(minutes=4)\n    # The applied prefix is chronological, not append-time or market-cursor derived.\n    assert lifecycle_watermark([first]) != lifecycle_watermark([first, late])\n    assert lifecycle_watermark([first]) != lifecycle_watermark([late])\n\n\ndef test_7_lifecycle_applied_prefix_advances_only_when_event_is_consumed() -> None:\n    g = _geometry("BUY", targets=("120",))\n    event = _event(3, {"type": "move_to_break_even", "target": "all", "value": None}, key="future")\n    state = _run(g, [_bar(0, high="101", low="99"), _bar(1, high="102", low="99")], events=[event])\n    assert state.market_cursor == BASE + timedelta(minutes=1)\n    assert state.lifecycle_applied_count == 0\n    state = _run(\n        g,\n        [_bar(2, high="102", low="99"), _bar(3, high="102", low="101")],\n        events=[event],\n        state=state,\n        full=False,\n    )\n    assert state.lifecycle_applied_count == 1\n    assert state.effective_stop == Decimal("100")\n\n\n''',
)
