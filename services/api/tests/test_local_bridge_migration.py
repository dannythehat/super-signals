from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_local_bridge_migration_is_the_single_head() -> None:
    api_root = Path(__file__).resolve().parents[1]
    script = ScriptDirectory.from_config(Config(str(api_root / "alembic.ini")))

    assert script.get_current_head() == "0126_local_mt5_bridge"


def test_local_bridge_revision_fits_alembic_version_column() -> None:
    assert len("0126_local_mt5_bridge") <= 32
