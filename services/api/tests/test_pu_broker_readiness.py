"""Read-only PU broker readiness preflight tests."""
from app.pu_broker_readiness import assess_broker_preflight


def _inputs():
    return {
        "identity": {"login": 36394935, "server": "PUPrime-Live 6", "goldSymbol": "XAUUSD.s"},
        "account": {
            "balance": 1000.0, "equity": 950.0, "currency": "USD",
            "tradeAllowed": True,
        },
        "symbol": {
            "symbol": "XAUUSD.s", "minVolume": 0.01, "volumeStep": 0.01,
        },
        "positions": [],
        "orders": [],
    }


def test_correct_account_with_funds_passes_broker_only():
    result = assess_broker_preflight(**_inputs())
    assert result["broker_preflight"] == "passed"
    assert result["actual_provider_order_routing_verified"] is False
    assert result["pu_prime_copy_follower_test_verified"] is False


def test_zero_balance_fails_closed():
    data = _inputs()
    data["account"]["balance"] = 0
    data["account"]["equity"] = 0
    result = assess_broker_preflight(**data)
    assert result["broker_preflight"] == "blocked"
    assert "broker_account_not_funded" in result["problems"]


def test_account_switch_fails_closed():
    data = _inputs()
    data["identity"]["login"] = 900001
    result = assess_broker_preflight(**data)
    assert result["broker_preflight"] == "blocked"
    assert "unexpected_mt5_login" in result["problems"]


def test_disabled_trading_fails_closed():
    data = _inputs()
    data["account"]["tradeAllowed"] = False
    assert "trading_not_allowed" in assess_broker_preflight(**data)["problems"]


def test_wrong_symbol_fails_closed():
    data = _inputs()
    data["symbol"]["symbol"] = "XAUUSD"
    assert "unexpected_gold_symbol" in assess_broker_preflight(**data)["problems"]


def test_open_positions_reported_not_mutated():
    data = _inputs()
    data["positions"] = [{"id": "1123"}, {"id": "1124"}]
    result = assess_broker_preflight(**data)
    assert result["open_broker_positions"] == 2
    assert result["actual_provider_order_routing_verified"] is False
