import inspect

from app.shadow_trading_v4 import ShadowTradeManager


def test_provider_lab_does_not_start_metaapi_market_stream() -> None:
    source = inspect.getsource(ShadowTradeManager.start)
    assert "_run_stream" not in source
    assert "MetaApi" not in source
    assert "super-signals-shadow-public-gold" in source


def test_provider_lab_quote_poll_never_reads_owner_broker() -> None:
    source = inspect.getsource(ShadowTradeManager.poll_once)
    assert "_broker_account" not in source
    assert "read_symbol_price" not in source
    assert "_public_bid_ask" in source
    assert 'quote_mode="snapshot_poll"' in source


def test_provider_lab_database_evaluation_runs_off_event_loop() -> None:
    source = inspect.getsource(ShadowTradeManager._evaluate_all)
    assert "asyncio.to_thread" in source
