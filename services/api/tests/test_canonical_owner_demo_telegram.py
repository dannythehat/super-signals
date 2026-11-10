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


def test_confirmed_duplicate_cleanup_deletes_only_the_proved_message_and_records_receipt(monkeypatch):
    from unittest.mock import MagicMock
    from app import telegram_publisher_canonical as publication
    manager = object.__new__(publication.CanonicalTelegramPublisherManager)
    manager._bot_token = 'test-only'
    manager._destination_chat_id = '-100123'
    session = MagicMock()
    session.__enter__.return_value = session
    session.execute.return_value.mappings.return_value.all.return_value = [
        {'id': '54e69aec-e916-4cf7-ae4e-8ff1a6a411ca', 'telegram_message_id': 1203,
         'destination_chat_id': '-100123'}]
    manager._session_factory = lambda: session
    api = MagicMock(return_value=True)
    monkeypatch.setattr(publication, '_bot_api_call', api)
    manager._cleanup_owner_demo_duplicate_events_safely()
    api.assert_called_once_with('test-only', 'deleteMessage', {'chat_id': '-100123', 'message_id': 1203})
    assert session.add.call_args.args[0].event_type == 'telegram.owner_demo_duplicate_removed'
    session.commit.assert_called_once()


def test_cleanup_keeps_unproven_posts(monkeypatch):
    from unittest.mock import MagicMock
    from app import telegram_publisher_canonical as publication
    manager = object.__new__(publication.CanonicalTelegramPublisherManager)
    manager._bot_token, manager._destination_chat_id = 'test-only', '-100123'
    session = MagicMock()
    session.__enter__.return_value = session
    manager._session_factory = lambda: session
    api = MagicMock()
    monkeypatch.setattr(publication, '_bot_api_call', api)
    session.execute.return_value.mappings.return_value.all.return_value = []
    manager._cleanup_owner_demo_duplicate_events_safely()
    api.assert_not_called()
    session.commit.assert_not_called()
