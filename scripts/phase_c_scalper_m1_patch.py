from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "services" / "api"


def replace(path: Path, old: str, new: str, *, count: int | None = None) -> None:
    text = path.read_text(encoding="utf-8")
    found = text.count(old)
    expected = 1 if count is None else count
    if found != expected:
        raise RuntimeError(f"{path}: expected {expected} occurrences, found {found}: {old[:120]!r}")
    path.write_text(text.replace(old, new), encoding="utf-8")


fair = API / "app" / "provider_fairness.py"
replace(
    fair,
    "Scalper-style providers remain observed but are\noutside the score/promotion funnel; AIDY M1 is canonical market truth for intraday/swing.",
    "Scalper, intraday, and swing providers use PIT-safe AIDY M1 as canonical market truth.\nScalpers additionally fail closed when M1 cannot establish intrabar TP/SL ordering.",
)
replace(
    fair,
    '    if normalized == "scalper":\n        return "unsupported"\n    if normalized in {"intraday", "swing_or_sparse"}:\n        return "aidy_m1"',
    '    if normalized in {"scalper", "intraday", "swing_or_sparse"}:\n        return "aidy_m1"',
)
replace(
    fair,
    '    if normalized_style == "scalper":\n        return False, "unsupported_style_scalper"\n    if normalized_style in {"intraday", "swing_or_sparse"}:\n        if mode != "aidy_m1":\n            return False, "aidy_m1_required_for_style"\n        return True, None',
    '    if normalized_style in {"scalper", "intraday", "swing_or_sparse"}:\n        if mode != "aidy_m1":\n            return False, "aidy_m1_required_for_style"\n        return True, None',
)

v4 = API / "app" / "shadow_trading_v4.py"
replace(
    v4,
    "AIDY is an authenticated read-only provider, never a shared database. Intraday and\nswing shadow trades are resolved incrementally from admitted AIDY M1 OHLC. Scalper\nmessages continue to be captured by the normal ingestion pipeline, but their shadow\nrows are explicitly excluded from scoring/promotion and need no market-resolution read.",
    "AIDY is an authenticated read-only provider, never a shared database. Scalper,\nintraday, and swing shadow trades are resolved incrementally from admitted AIDY M1\nOHLC. Scalpers remain score-ineligible whenever M1 cannot prove intrabar ordering.",
)
replace(v4, '_AIDY_STYLES = {"intraday", "swing_or_sparse"}', '_AIDY_STYLES = {"scalper", "intraday", "swing_or_sparse"}')
old_method = '''    def _enforce_scalper_exclusion_sync(self) -> int:\n        with self._session_factory() as session:\n            result = session.execute(\n                text(\n                    """\n                    UPDATE shadow_trades\n                    SET score_eligible=false,\n                        score_exclusion_reason='unsupported_style_scalper',\n                        updated_at=now()\n                    WHERE provider_style='scalper'\n                      AND (\n                        score_eligible\n                        OR score_exclusion_reason IS DISTINCT FROM 'unsupported_style_scalper'\n                      )\n                    """\n                )\n            )\n            session.commit()\n            return int(result.rowcount or 0)\n'''
new_method = '''    def _prepare_scalper_m1_resolution_sync(self) -> int:\n        \"\"\"Retire only the old blanket scalper block; never erase other exclusions.\"\"\"\n        with self._session_factory() as session:\n            result = session.execute(\n                text(\n                    """\n                    UPDATE shadow_trades\n                    SET score_eligible=false,\n                        score_exclusion_reason='outcome_pending_aidy_m1',\n                        aidy_score_blocked=false,\n                        updated_at=now()\n                    WHERE provider_style='scalper'\n                      AND score_exclusion_reason='unsupported_style_scalper'\n                      AND NOT COALESCE(aidy_terminal,false)\n                      AND provider_profile_pit_status='resolved'\n                      AND aidy_original_geometry IS NOT NULL\n                    """\n                )\n            )\n            session.commit()\n            return int(result.rowcount or 0)\n'''
replace(v4, old_method, new_method)
replace(v4, 'await asyncio.to_thread(self._enforce_scalper_exclusion_sync)', 'await asyncio.to_thread(self._prepare_scalper_m1_resolution_sync)')
replace(
    v4,
    "        # This is a policy gate, not market inference: future scalper rows are kept for\n        # audit/message observation but cannot silently re-enter scoring or promotion.\n",
    "        # Move legacy blanket-excluded scalpers into the same PIT-safe AIDY M1\n        # resolution queue. Eligibility is granted only after deterministic closure.\n",
)
replace(
    v4,
    '            if str(row["provider_style"]) not in _AIDY_STYLES | {"scalper"}',
    '            if str(row["provider_style"]) not in _AIDY_STYLES',
)

for relative in ("app/shadow_trading_v5.py", "app/shadow_trading_service_v4.py"):
    path = API / relative
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        '                else "unsupported_style_scalper"\n                if provider_style == "scalper"\n                else "market_data_not_observed"',
        '                else "outcome_pending_aidy_m1"\n                if provider_style == "scalper"\n                else "market_data_not_observed"',
    )
    text = text.replace(
        '                    else "unsupported_style_scalper"\n                    if provider_style == "scalper"\n                    else "bare_profile_entry_delay_over_scoring_gate"',
        '                    else "outcome_pending_aidy_m1"\n                    if provider_style == "scalper"\n                    else "bare_profile_entry_delay_over_scoring_gate"',
    )
    if relative.endswith("shadow_trading_v5.py"):
        text = text.replace(
            "await asyncio.to_thread(self._enforce_scalper_exclusion_sync)",
            "await asyncio.to_thread(self._prepare_scalper_m1_resolution_sync)",
        )
    if "unsupported_style_scalper" in text:
        raise RuntimeError(f"{path}: old blanket scalper exclusion remains")
    path.write_text(text, encoding="utf-8")

resolver = API / "app" / "aidy_shadow_resolver.py"
replace(resolver, '_SUPPORTED_STYLES = {"intraday", "swing_or_sparse"}', '_SUPPORTED_STYLES = {"scalper", "intraday", "swing_or_sparse"}')
replace(
    resolver,
    "    full_replay: bool,\n) -> ResolutionState:",
    "    full_replay: bool,\n    strict_intrabar_ambiguity: bool = False,\n) -> ResolutionState:",
)
old_same_bar = '''            if stop_hit and hit_legs:\n                _close_at_stop(\n                    state,\n                    geometry=geometry,\n                    reason="aidy_m1_ambiguous_worst_case_stop",\n                    when=bar_end,\n                    bar=bar,\n                )\n                state.note = "within_bar_sl_and_tp_touched_stop_assumed_first"\n                state.market_cursor = bar_open\n                return state\n'''
new_same_bar = '''            if stop_hit and hit_legs:\n                if strict_intrabar_ambiguity:\n                    _block(\n                        state,\n                        reason="aidy_m1_scalper_intrabar_sequence_ambiguous",\n                        when=bar_end,\n                        note="scalper_m1_cannot_order_stop_vs_target_inside_same_bar",\n                        bar=bar,\n                    )\n                else:\n                    _close_at_stop(\n                        state,\n                        geometry=geometry,\n                        reason="aidy_m1_ambiguous_worst_case_stop",\n                        when=bar_end,\n                        bar=bar,\n                    )\n                    state.note = "within_bar_sl_and_tp_touched_stop_assumed_first"\n                state.market_cursor = bar_open\n                return state\n'''
replace(resolver, old_same_bar, new_same_bar)
replace(
    resolver,
    "WHERE provider_style IN ('intraday','swing_or_sparse')",
    "WHERE provider_style IN ('scalper','intraday','swing_or_sparse')",
)
replace(
    resolver,
    "                            'market_data_not_observed',\n                            'aidy_m1_revalidation_required',\n                            'outcome_pending_aidy_m1'",
    "                            'market_data_not_observed',\n                            'aidy_m1_revalidation_required',\n                            'unsupported_style_scalper',\n                            'outcome_pending_aidy_m1'",
)
replace(
    resolver,
    '        revalidation = trade.get("score_exclusion_reason") in {\n            "market_data_not_observed",\n            "aidy_m1_revalidation_required",\n        }',
    '        revalidation = trade.get("score_exclusion_reason") in {\n            "market_data_not_observed",\n            "aidy_m1_revalidation_required",\n            "unsupported_style_scalper",\n        }',
)
replace(
    resolver,
    "            sibling_entries=sibling_entries,\n            full_replay=full_replay,\n        )",
    "            sibling_entries=sibling_entries,\n            full_replay=full_replay,\n            strict_intrabar_ambiguity=(str(trade.get(\"provider_style\")) == \"scalper\"),\n        )",
)

fair_test = API / "tests" / "test_provider_fairness_v2.py"
replace(
    fair_test,
    '''def test_scalpers_are_out_and_intraday_swing_require_aidy_m1():\n    assert score_eligibility(style="scalper", quote_mode="stream_tick") == (\n        False,\n        "unsupported_style_scalper",\n    )\n    assert score_eligibility(style="scalper", quote_mode="aidy_m1") == (\n        False,\n        "unsupported_style_scalper",\n    )\n''',
    '''def test_scalpers_intraday_and_swing_require_aidy_m1():\n    assert score_eligibility(style="scalper", quote_mode="stream_tick") == (\n        False,\n        "aidy_m1_required_for_style",\n    )\n    assert score_eligibility(style="scalper", quote_mode="aidy_m1") == (True, None)\n''',
)

truth_test = API / "tests" / "test_aidy_provider_lab_market_truth.py"
replace(
    truth_test,
    "def _run(g: OriginalGeometry, bars: list[AidyM1Bar], *, events=None, posted=BASE, state=None, full=True):",
    "def _run(g: OriginalGeometry, bars: list[AidyM1Bar], *, events=None, posted=BASE, state=None, full=True, strict=False):",
)
replace(
    truth_test,
    "        sibling_entries=[(g.entry_index, g.entry_high if g.side == \"BUY\" else g.entry_low)],\n        full_replay=full,\n    )",
    "        sibling_entries=[(g.entry_index, g.entry_high if g.side == \"BUY\" else g.entry_low)],\n        full_replay=full,\n        strict_intrabar_ambiguity=strict,\n    )",
)
append = '''\n\ndef test_phase_c_scalper_same_bar_tp_sl_is_excluded_not_guessed() -> None:\n    for side, stop, target in (("BUY", "95", "105"), ("SELL", "105", "95")):\n        g = _geometry(side, stop=stop, targets=(target,))\n        state = _run(g, [_bar(0, high="106", low="94")], strict=True)\n        assert state.terminal is True\n        assert state.score_eligible is False\n        assert state.score_block_reason == "aidy_m1_scalper_intrabar_sequence_ambiguous"\n        assert state.legs[0].realized_r == Decimal("0")\n\n\ndef test_phase_c_scalper_unambiguous_m1_target_is_scoreable() -> None:\n    g = _geometry("BUY", targets=("105",))\n    state = _run(g, [_bar(0, high="104", low="99"), _bar(1, high="106", low="101")], strict=True)\n    assert state.terminal is True\n    assert state.score_eligible is True\n    assert state.legs[0].exit_reason == "target"\n    assert state.legs[0].realized_r == Decimal("1")\n'''
text = truth_test.read_text(encoding="utf-8")
if "test_phase_c_scalper_same_bar_tp_sl_is_excluded_not_guessed" in text:
    raise RuntimeError("Phase C tests already present")
truth_test.write_text(text + append, encoding="utf-8")

contract_test = API / "tests" / "test_phase_c_scalper_m1_contract.py"
contract_test.write_text(
    '''from pathlib import Path\n\nROOT = Path(__file__).resolve().parents[1]\n\n\ndef test_scalper_runtime_uses_aidy_m1_and_replays_old_blanket_exclusions():\n    resolver=(ROOT/"app"/"aidy_shadow_resolver.py").read_text(encoding="utf-8")\n    v4=(ROOT/"app"/"shadow_trading_v4.py").read_text(encoding="utf-8")\n    v5=(ROOT/"app"/"shadow_trading_v5.py").read_text(encoding="utf-8")\n    fairness=(ROOT/"app"/"provider_fairness.py").read_text(encoding="utf-8")\n    assert '_SUPPORTED_STYLES = {"scalper", "intraday", "swing_or_sparse"}' in resolver\n    assert "provider_style IN ('scalper','intraday','swing_or_sparse')" in resolver\n    assert '"unsupported_style_scalper"' in resolver  # backlog is an allowed replay trigger\n    assert 'aidy_m1_scalper_intrabar_sequence_ambiguous' in resolver\n    assert '_prepare_scalper_m1_resolution_sync' in v4\n    assert '_enforce_scalper_exclusion_sync' not in v4 + v5\n    assert 'return "unsupported"' not in fairness\n\n\ndef test_phase_c_does_not_touch_live_execution_or_risk():\n    migration=(ROOT/"migrations"/"versions"/"0077_enable_scalper_aidy_m1.py").read_text(encoding="utf-8")\n    assert "outcome_pending_aidy_m1" in migration\n    assert "provider_profile_pit_status = 'resolved'" in migration\n    assert "aidy_original_geometry IS NOT NULL" in migration\n    assert "live-risk" in migration\n    assert "UPDATE accounts" not in migration\n    assert "UPDATE user_trading_settings" not in migration\n''',
    encoding="utf-8",
)

print("phase_c_scalper_m1_patch=PASS")
