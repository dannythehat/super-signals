"""Regression guard for the AIDY/live-trading database isolation contract.

AIDY research once contended with live execution badly enough to stop real
trading. Two separate protections were added in response, and BOTH are easy to
delete by accident in a merge:

1. The live engine is deliberately bounded so a burst of research or
   reconciliation work cannot swamp the small Render Postgres instance and
   starve the trading listener, and recycles connections so it recovers from
   Render/Postgres resets.

2. AIDY research runs in its own lane with one connection and no overflow, with
   server-side statement/lock/idle-transaction timeouts so a runaway research
   query dies in the research lane instead of taking production with it.

A third property matters just as much: the research lane must not be tightened
back to the values that caused AIDY to starve itself. Commit a61e665d ("Fix AIDY
research self-starvation") moved statement_timeout 5000 -> 15000 and pool_timeout
2 -> 30 precisely because the tighter settings stopped research completing. Any
change proposing 5000/1 again is reintroducing a known-broken configuration.

These tests inspect configuration only. They open no connection.
"""
from __future__ import annotations

import inspect

from app import db as db_module


def _source(func: object) -> str:
    return inspect.getsource(func)


def test_live_engine_pool_stays_bounded() -> None:
    """Removing these bounds lets live connections rise to SQLAlchemy defaults
    (pool_size=5 + max_overflow=10 = 15) on the instance that already failed."""
    source = _source(db_module.get_engine)
    for setting in ("pool_size", "max_overflow", "pool_timeout", "pool_recycle"):
        assert setting in source, (
            f"live engine lost {setting}: the bounded pool exists so research "
            "cannot starve the trading listener. Do not remove it."
        )


def test_research_lane_is_single_connection_with_no_overflow() -> None:
    source = _source(db_module.get_research_engine)
    assert "pool_size=1" in source
    assert "max_overflow=0" in source


def test_research_lane_keeps_server_side_timeouts() -> None:
    source = _source(db_module.get_research_engine)
    for guard in ("statement_timeout", "lock_timeout", "idle_in_transaction_session_timeout"):
        assert guard in source, f"research lane lost {guard}"


def test_research_timeouts_remain_tunable_without_a_deploy() -> None:
    """Hardcoding these removes the only knob available during an incident."""
    source = _source(db_module.get_research_engine)
    assert "AIDY_RESEARCH_DB_STATEMENT_TIMEOUT_MS" in source
    assert "AIDY_RESEARCH_DB_POOL_TIMEOUT_SECONDS" in source


def test_research_lane_does_not_regress_to_self_starvation_defaults() -> None:
    """a61e665d raised these because 5000ms/2s starved AIDY research."""
    source = _source(db_module.get_research_engine)
    assert "AIDY_RESEARCH_DB_STATEMENT_TIMEOUT_MS\", 15000" in source
    assert "AIDY_RESEARCH_DB_POOL_TIMEOUT_SECONDS\", 30" in source


def test_research_connections_stay_identifiable_in_pg_stat_activity() -> None:
    """Without the tag, AIDY connections cannot be told apart from live ones
    while diagnosing a live incident."""
    assert "super-signals-aidy-research" in _source(db_module.get_research_engine)


def test_live_and_research_engines_are_separate_objects() -> None:
    assert db_module.get_engine is not db_module.get_research_engine
    assert "get_research_engine()" in _source(db_module.get_research_session_factory)
