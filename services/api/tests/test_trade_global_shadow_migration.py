"""TRADE GLOBAL must be returned to shadow, and never at the cost of a live position.

Context (2026-09-23): migration 0114 moved TRADE GLOBAL to `testing` at 11:03 UTC; it
opened 19 broker positions and lost about 151 USD before a name-matching dispatch gate
stopped it. The follow-up that set it back to shadow collided with 0114 as a second
Alembic head and never deployed. These tests pin the repair and its safety conditions.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

API = Path(__file__).resolve().parents[1]
MIGRATION = API / "migrations" / "versions" / "0116_shadow_trade_global.py"
SOURCE = MIGRATION.read_text(encoding="utf-8")


def test_chains_after_the_real_head_so_it_cannot_collide_again() -> None:
    assert 'revision: str = "0116_shadow_trade_global"' in SOURCE
    assert 'down_revision: str | None = "0115_provider_playbook_guard"' in SOURCE
    assert len("0116_shadow_trade_global") <= 32  # alembic_version is varchar(32)


def test_there_is_exactly_one_alembic_head() -> None:
    """A second head is exactly what stopped the previous fix from deploying."""
    config = Config(str(API / "alembic.ini"))
    config.set_main_option("script_location", str(API / "migrations"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert heads == ["0119_profit_only_provider_status"], heads


def test_targets_trade_global_by_chat_id_not_by_display_name() -> None:
    """The name-matching gate breaks if the channel is renamed; the chat id does not."""
    assert "TRADE_GLOBAL_CHAT_ID = -1003925988158" in SOURCE
    assert "SET status='shadow'" in SOURCE


def test_refuses_to_strand_a_live_position() -> None:
    """Shadow also blocks close instructions, so both exposure checks must gate it."""
    assert "p.status IN ('open','planned','pending')" in SOURCE
    assert "DEAL_ENTRY_IN" in SOURCE and "DEAL_ENTRY_OUT" in SOURCE
    assert "x.net > 0" in SOURCE
    assert "if live_rows or net_open_at_broker:" in SOURCE
    assert "telegram.source_status_change_skipped" in SOURCE


def test_skipping_never_raises_and_takes_startup_down() -> None:
    upgrade = SOURCE.split("def upgrade()", 1)[1].split("def downgrade()", 1)[0]
    assert "raise" not in upgrade
