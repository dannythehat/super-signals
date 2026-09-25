"""Regression cover for the orphaned-fill path.

Three real Owner positions (broker ids 1845153776, 1867467917, 2002783268) were filled at
Vantage, recorded by the application as ``status='error'`` with
``close_reason='broker_filled_position_not_visible'``, and then never revisited. They
stayed live at the broker for up to four weeks: untracked by management, unreachable by
the owner close control, and absent from every open-position view.

These tests pin the settlement that resolves them and the invariant that catches the next
one regardless of which code path creates it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from app.broker_fill_settlement import (
    ADOPT_OPEN,
    AWAIT_EVIDENCE,
    SETTLE_CLOSED,
    BrokerFillSettlementService,
    BrokerPositionEvidence,
    _evidence_from_row,
    decide_settlement,
)

OWNER = UUID("ea604df2-f8ee-47d1-bc51-f0078dbf160d")
ACCOUNT = UUID("161613ee-0155-43b9-8c86-40def40101c8")


def _evidence(**kwargs: object) -> BrokerPositionEvidence:
    base: dict[str, object] = {
        "broker_position_id": "1845153776",
        "entry_volume": Decimal("0.01"),
        "exit_volume": Decimal("0"),
        "first_entry_at": datetime(2026, 8, 25, 6, 10, 29, tzinfo=UTC),
    }
    base.update(kwargs)
    return BrokerPositionEvidence(**base)  # type: ignore[arg-type]


def test_entry_deal_without_exit_is_adopted_open() -> None:
    """The exact shape of all three stranded Owner positions: the broker filled them and
    never closed them, so they are open and must rejoin management."""
    assert decide_settlement(_evidence()).action == ADOPT_OPEN


def test_no_entry_deal_is_left_alone() -> None:
    """Without an ingested entry deal there is no evidence, and inventing an open
    position would repeat the guess the pending reconciler correctly refused to make."""
    decision = decide_settlement(_evidence(entry_volume=Decimal("0")))
    assert decision.action == AWAIT_EVIDENCE
    assert decision.reason == "no_entry_deal_ingested"


def test_partial_exit_stays_open_because_exposure_remains() -> None:
    """Deal *count* would call this closed. Volume is what decides: 0.01 of 0.02 is still
    live at the broker and must stay reachable."""
    decision = decide_settlement(
        _evidence(entry_volume=Decimal("0.02"), exit_volume=Decimal("0.01"))
    )
    assert decision.action == ADOPT_OPEN


def test_covering_exit_volume_settles_closed() -> None:
    assert (
        decide_settlement(
            _evidence(entry_volume=Decimal("0.02"), exit_volume=Decimal("0.02"))
        ).action
        == SETTLE_CLOSED
    )


def test_over_covering_exit_volume_settles_closed() -> None:
    assert (
        decide_settlement(
            _evidence(entry_volume=Decimal("0.02"), exit_volume=Decimal("0.03"))
        ).action
        == SETTLE_CLOSED
    )


def test_entry_price_is_volume_weighted_from_broker_deals() -> None:
    """Real notional from broker_deals for 1845153776: 46.50 over 0.01 lots."""
    evidence = _evidence_from_row(
        "1845153776",
        {
            "entry_volume": Decimal("0.01"),
            "exit_volume": Decimal("0"),
            "entry_notional": Decimal("46.50"),
            "exit_notional": None,
            "first_entry_at": datetime(2026, 8, 25, 6, 10, 29, tzinfo=UTC),
            "last_exit_at": None,
            "realised_cash": Decimal("0"),
        },
    )
    assert evidence.entry_price == Decimal("4650.00")
    assert evidence.exit_price is None


class _StubResult:
    def __init__(self, rows: list[dict] | None = None, rowcount: int = 1) -> None:
        self._rows = rows or []
        self.rowcount = rowcount

    def mappings(self):  # noqa: ANN201
        return self

    def all(self):  # noqa: ANN201
        return self._rows

    def one(self):  # noqa: ANN201
        return self._rows[0]


class _StubSession:
    """Dispatches on statement text; records every UPDATE and AuditEvent."""

    def __init__(self, store: dict) -> None:
        self._store = store

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, *_exc) -> bool:  # noqa: ANN002
        return False

    def execute(self, statement, params=None):  # noqa: ANN001, ANN201
        sql = str(statement)
        if "UPDATE positions" in sql:
            self._store.setdefault("updates", []).append((sql, params))
            return _StubResult(rowcount=self._store.get("update_rowcount", 1))
        if "FROM positions AS p" in sql:
            return _StubResult(self._store["stranded"])
        if "FROM broker_deals" in sql:
            key = str((params or {}).get("broker_position_id"))
            return _StubResult([self._store["evidence"][key]])
        raise AssertionError(f"unexpected statement: {sql[:120]}")

    def add(self, obj) -> None:  # noqa: ANN001
        self._store.setdefault("audits", []).append(obj)

    def commit(self) -> None:
        self._store["commits"] = self._store.get("commits", 0) + 1


def _stranded(broker_position_id: str, *, known_locally: bool = True) -> dict:
    """A stranded tranche. ``known_locally=False`` mimics a row that never received a
    ``broker_position_id`` (the ambiguous-placement path): resolution then depends
    entirely on ``broker_client_id`` matching against ``broker_deals``."""
    return {
        "id": uuid4(),
        "user_id": OWNER,
        "signal_id": uuid4(),
        "broker_position_id": broker_position_id if known_locally else None,
        "resolved_broker_position_id": broker_position_id,
        "status": "error",
        "close_reason": "broker_filled_position_not_visible",
        "mt5_account_id": ACCOUNT,
    }


def _deal_row(
    entry_volume: str, exit_volume: str, notional: str, *, realised_cash: str = "0"
) -> dict:
    return {
        "entry_volume": Decimal(entry_volume),
        "exit_volume": Decimal(exit_volume),
        "entry_notional": Decimal(notional),
        "exit_notional": None,
        "first_entry_at": datetime(2026, 8, 25, 6, 10, 29, tzinfo=UTC),
        "last_exit_at": None,
        "realised_cash": Decimal(realised_cash),
    }


def test_the_three_real_orphans_are_all_adopted_open() -> None:
    """End-to-end over the exact production rows: every one returns to 'open', which is
    the status the owner close control selects on (owner_manual_close.py)."""
    store = {
        "stranded": [
            _stranded("1845153776"),
            _stranded("1867467917"),
            _stranded("2002783268"),
        ],
        "evidence": {
            "1845153776": _deal_row("0.01", "0", "46.50"),
            "1867467917": _deal_row("0.01", "0", "46.35"),
            "2002783268": _deal_row("0.01", "0", "43.53"),
        },
    }
    service = BrokerFillSettlementService(lambda: _StubSession(store))  # type: ignore[arg-type]
    result = service.settle_once()

    assert result.considered == 3
    assert result.adopted_open == 3
    assert result.settled_closed == 0
    assert result.awaiting_evidence == 0

    updates = store["updates"]
    assert len(updates) == 3
    for sql, _params in updates:
        assert "status='open'" in sql
        assert "close_reason=NULL" in sql
        # Only a row still stranded may be adopted; never re-open a settled position.
        assert "AND status='error'" in sql

    audits = store["audits"]
    assert len(audits) == 3
    assert {item.event_type for item in audits} == {"mt5.broker_fill_settled_open"}
    for item in audits:
        assert item.payload["broker_contacted"] is False
        assert item.payload["trade_action_created"] is False


def test_positions_with_no_local_broker_position_id_settle_via_broker_client_id() -> None:
    """Eleven real Owner positions (e.g. broker_client_id SS_842d397c7347_1) were marked
    'error' before the broker ever confirmed a position id - an ambiguous placement
    outcome or a mapping-validation failure after the order had already filled - so
    broker_position_id itself is null on the local row. broker_client_id is assigned
    locally before submission and is always present, so it is the only way back to the
    broker's own deal history for these. This trade actually closed for a real, small
    profit that never reached pnl_amount, performance_trade_outcomes or Telegram."""
    store = {
        "stranded": [_stranded("2071236890", known_locally=False)],
        "evidence": {"2071236890": _deal_row("0.01", "0.01", "43.376", realised_cash="4.84")},
    }
    service = BrokerFillSettlementService(lambda: _StubSession(store))  # type: ignore[arg-type]
    result = service.settle_once()

    assert result.settled_closed == 1
    updates = store["updates"]
    assert len(updates) == 1
    sql, params = updates[0]
    assert "status='closed'" in sql
    assert "broker_position_id=COALESCE(broker_position_id,:broker_position_id)" in sql
    assert params["broker_position_id"] == "2071236890"
    assert params["pnl_amount"] == Decimal("4.84")


def test_settlement_leaves_a_fill_with_no_deals_untouched() -> None:
    store = {
        "stranded": [_stranded("9999999999")],
        "evidence": {"9999999999": _deal_row("0", "0", "0")},
    }
    service = BrokerFillSettlementService(lambda: _StubSession(store))  # type: ignore[arg-type]
    result = service.settle_once()

    assert result.awaiting_evidence == 1
    assert result.changed == 0
    assert "updates" not in store


def test_settlement_never_reaches_the_broker() -> None:
    """Settlement corrects application state from deals that are already ingested. If it
    ever gains a broker gateway it can open or close real positions, so the boundary is
    pinned here rather than left to review."""
    source = Path(__file__).resolve().parents[1] / "app" / "broker_fill_settlement.py"
    text = source.read_text()
    for forbidden in ("metaapi", "MetaApi", "trade_gateway", "gateway"):
        assert forbidden not in text, f"settlement must not reference {forbidden}"


def test_adopted_status_is_exactly_what_the_owner_close_control_selects() -> None:
    """The orphans were unclosable because owner_manual_close selects status='open'.
    Adoption is only a real fix while these two agree, so the coupling is pinned."""
    control = (
        Path(__file__).resolve().parents[1] / "app" / "owner_manual_close.py"
    ).read_text()
    assert "p.status='open'" in control

    settlement = (
        Path(__file__).resolve().parents[1] / "app" / "broker_fill_settlement.py"
    ).read_text()
    assert "SET status='open'" in settlement


class _CountingSettlement:
    def __init__(self) -> None:
        self.calls = 0

    def settle_once(self):  # noqa: ANN201
        self.calls += 1
        from app.broker_fill_settlement import SettlementResult

        return SettlementResult(considered=1, adopted_open=1)

    def orphaned_positions(self):  # noqa: ANN201
        return ()


def test_settlement_is_throttled_between_reconciler_polls() -> None:
    """The reconciler polls every few seconds; stranded fills are rare. Settlement must
    not run a full sweep on every poll."""
    from app.unified_pending_reconciler import UnifiedPendingReconciler

    reconciler = object.__new__(UnifiedPendingReconciler)
    reconciler._settlement = _CountingSettlement()
    reconciler._settlement_interval_seconds = 60
    reconciler._last_settlement_at = None

    assert reconciler.settle_stranded_fills(now=1000.0).adopted_open == 1
    assert reconciler.settle_stranded_fills(now=1030.0).adopted_open == 0
    assert reconciler.settle_stranded_fills(now=1059.9).adopted_open == 0
    assert reconciler.settle_stranded_fills(now=1060.0).adopted_open == 1
    assert reconciler._settlement.calls == 2


def test_an_update_that_matches_nothing_is_not_audited() -> None:
    """The UPDATE is guarded on status='error', so a row a concurrent pass already
    settled matches nothing. Auditing anyway would record a settlement that never
    happened - the audit trail is the evidence this system is judged on."""
    store = {
        "stranded": [_stranded("1845153776")],
        "evidence": {"1845153776": _deal_row("0.01", "0", "46.50")},
        "update_rowcount": 0,
    }
    service = BrokerFillSettlementService(lambda: _StubSession(store))  # type: ignore[arg-type]
    result = service.settle_once()

    assert result.adopted_open == 1          # the pass still attempted it
    assert len(store["updates"]) == 1        # the guarded UPDATE ran
    assert "audits" not in store             # but nothing was recorded


def test_a_settled_close_that_matches_nothing_is_not_audited() -> None:
    store = {
        "stranded": [_stranded("1792311887")],
        "evidence": {"1792311887": _deal_row("0.14", "0.14", "607.32")},
        "update_rowcount": 0,
    }
    service = BrokerFillSettlementService(lambda: _StubSession(store))  # type: ignore[arg-type]
    service.settle_once()
    assert "audits" not in store


def test_an_unknown_rowcount_is_still_audited() -> None:
    """A driver reporting -1 means 'unknown', not 'nothing happened'. Dropping the
    audit there would lose a real settlement."""
    store = {
        "stranded": [_stranded("1845153776")],
        "evidence": {"1845153776": _deal_row("0.01", "0", "46.50")},
        "update_rowcount": -1,
    }
    service = BrokerFillSettlementService(lambda: _StubSession(store))  # type: ignore[arg-type]
    service.settle_once()
    assert len(store["audits"]) == 1
