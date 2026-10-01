"""The 1 October 2026 demo-compromise reporting override (migration 0117).

It is a reporting-layer correction: it may only INSERT one override row (and its audit
event) and must never touch broker evidence, balances, positions or startup.
"""

from __future__ import annotations

import importlib.util
from decimal import Decimal
from pathlib import Path

API = Path(__file__).resolve().parents[1]
PATH = API / "migrations" / "versions" / "0125_incident_20261001_override.py"
SOURCE = PATH.read_text(encoding="utf-8")
EARLIER = (API / "migrations" / "versions" / "0029_reporting_overrides.py").read_text(encoding="utf-8")


def _load():
    spec = importlib.util.spec_from_file_location("mig0125", PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_chains_after_0124_and_fits_alembic_version_column() -> None:
    m = _load()
    assert m.revision == "0125_incident_20261001_override"
    assert m.down_revision == "0124_post_exec_edit"
    assert len(m.revision) <= 32  # alembic_version is varchar(32)


def test_targets_the_same_owner_demo_account_as_the_26_august_override() -> None:
    m = _load()
    assert m.OWNER_DEMO_METAAPI_ACCOUNT_ID in EARLIER


def test_reviewed_amount_cutoff_and_reason_are_the_approved_ones() -> None:
    m = _load()
    assert Decimal(m.REVIEWED_CASH_PNL) == Decimal("384.84")
    assert m.REPORTING_DATE == "2026-10-01"
    # After the balance was restored (first seen 07:38 UTC) and after the last affected exit (07:22).
    assert m.CUTOFF_AT == "2026-10-01T07:45:00+00:00"
    # The reason must say plainly what the number is and is not.
    assert "unauthorised trading" in m.REASON
    assert "not made by Super Signals trades" in m.REASON
    assert "-$97.51" in m.REASON
    assert "Raw broker evidence is preserved" in m.REASON


def test_only_inserts_one_override_row_and_never_modifies_broker_evidence() -> None:
    upgrade = SOURCE.split("def upgrade()", 1)[1].split("def downgrade()", 1)[0]
    body = upgrade.upper()
    assert "INSERT INTO PERFORMANCE_REPORTING_OVERRIDES" in body
    assert "ON CONFLICT DO NOTHING" in body  # idempotent; a second run changes nothing
    for forbidden in ("UPDATE ", "DELETE ", "DROP ", "TRUNCATE", "BROKER_DEALS", "POSITIONS", "MT5_ACCOUNTS SET"):
        assert forbidden not in body.replace("FROM MT5_ACCOUNTS", ""), forbidden
    assert "raise" not in upgrade  # can never take startup down


def test_downgrade_removes_only_this_incident_row() -> None:
    downgrade = SOURCE.split("def downgrade()", 1)[1]
    assert "DELETE FROM performance_reporting_overrides WHERE incident_key=:key" in downgrade
    assert "broker_deals" not in downgrade


def test_override_uses_the_existing_non_restart_semantics() -> None:
    """A key not starting with 'restart-' excludes only pre-cutoff exits of that day, keeping later ones."""
    m = _load()
    assert not m.INCIDENT_KEY.startswith("restart-")
    assert len(m.INCIDENT_KEY) <= 96
