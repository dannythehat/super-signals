from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_widened_diagnostic_has_no_broker_mutation_imports():
    source = (ROOT / "app" / "provider_day11_widened_diagnostic.py").read_text(encoding="utf-8")
    forbidden = (
        "MetaApi",
        "execute_owner_demo_signal",
        "create_market",
        "create_limit",
        "close_position",
        "cancel_order",
        "risk_percent",
    )
    assert all(token not in source for token in forbidden)
