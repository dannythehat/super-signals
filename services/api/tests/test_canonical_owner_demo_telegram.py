"""Owner demo broker placements participate in the *active* canonical publisher."""

from app.telegram_publisher_canonical import (
    _OWNER_DEMO_PLACEMENT_EVENT,
    _confirmed_placement_sql,
)


def test_canonical_publisher_accepts_only_valid_broker_confirmations() -> None:
    clause = _confirmed_placement_sql("placed")
    assert "_demo_" in _OWNER_DEMO_PLACEMENT_EVENT
    assert "mt5.day38_route_new_trade" in clause
    assert "mt5.owner_demo_700207861_execution_success" in clause
    assert "placed.payload->>'outcome'='executed'" in clause
    assert "placed.payload->>'master_login'='700207861'" in clause
    assert "placed.payload->>'magic'='2601001'" in clause
    assert "placed.payload->>'owner_demo_only'='true'" in clause
    assert "placed.payload->>'orders_submitted_by_this_endpoint'='false'" in clause
    assert "broker_reported_deals" in clause


def test_sql_helper_uses_explicit_placement_alias() -> None:
    sql = _confirmed_placement_sql("owner_placement")
    assert "owner_placement.event_type=" in sql
    assert "owner_placement.payload->>'master_login'" in sql
    assert "placed.event_type=" not in sql
