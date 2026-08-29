"""Regression checks for the Day 14 shared-source API compatibility layer."""

from uuid import uuid4

from app.routes.telegram_sources import _shared_response
from app.telegram_source_service_day14 import Day14SharedTelegramSourceView


def test_day14_shared_source_view_matches_current_api_contract() -> None:
    source_id = uuid4()
    item = Day14SharedTelegramSourceView(
        source_id=source_id,
        chat_id=-1001234567890,
        title="Gold Signals",
        status="shadow",
        connection_status="connected",
        connected_reader_count=1,
        shadow_total=8,
        shadow_open=2,
        shadow_closed=6,
        shadow_wins=4,
        shadow_losses=2,
        shadow_return_percent="3.25",
    )

    response = _shared_response(item)

    assert response.source_id == source_id
    assert response.chat_id == -1001234567890
    assert response.title == "Gold Signals"
    assert response.status == "shadow"
    assert response.shadow_total == 8
    assert response.shadow_open == 2
    assert response.shadow_closed == 6
    assert response.shadow_wins == 4
    assert response.shadow_losses == 2
    assert response.shadow_return_percent == "3.25"
