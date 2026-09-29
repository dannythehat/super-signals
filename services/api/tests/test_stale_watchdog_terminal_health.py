from inspect import getsource

from app.stale_position_watchdog import StalePositionWatchdog


def test_watchdog_health_probe_is_read_only_and_resilient() -> None:
    source = getsource(StalePositionWatchdog._probe_terminal_isolated)
    assert "Day23Mt5ReadService" in source
    assert "PaperResilientMetaApiReadGateway" in source
    assert "read_owner_live_state" in source
    assert "MetaApiTradeGateway" not in source
    assert "close_position" not in source


def test_watchdog_probes_terminal_before_stale_position_scan() -> None:
    source = getsource(StalePositionWatchdog.poll_once)
    assert source.index("_probe_terminal_isolated") < source.index("_candidates")
    assert "asyncio.to_thread(self._probe_terminal_isolated)" in source
