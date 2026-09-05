from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_aidy_signal_revisions_do_not_mutate_shadow_state_outside_replay() -> None:
    source = (ROOT / "app" / "shadow_trading_service_v4.py").read_text(encoding="utf-8")
    marker = 'if provider_style in _AIDY_STYLES:\n                        return False'
    assert marker in source
    revision_block = source[source.index("if revision_index > 0:"):source.index("stop = _decimal")]
    aidy_branch = revision_block[revision_block.index("if provider_style in _AIDY_STYLES:"):]
    # The AIDY path exits before the legacy direct UPDATE. Same-message edits are
    # consumed from the append-only signal_revision lifecycle event by replay.
    assert aidy_branch.index("return False") < aidy_branch.index("UPDATE shadow_trades")


def test_aidy_management_is_append_only_handoff_to_replay() -> None:
    source = (ROOT / "app" / "shadow_trading_service_v4.py").read_text(encoding="utf-8")
    management = source[source.index("def record_management"):]
    assert "t.provider_style IN ('intraday','swing_or_sparse')" in management
    assert "if aidy_row is not None:\n            return True" in management
