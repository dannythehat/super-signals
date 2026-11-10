from uuid import uuid4
from unittest.mock import Mock

import pytest

from app.telegram_publisher_day34 import Day34TelegramPublisherManager, SummaryPublicationAttempt


@pytest.mark.asyncio
async def test_owner_account_snapshot_edits_the_existing_post(monkeypatch):
    publisher = object.__new__(Day34TelegramPublisherManager)
    publisher._bot_token = "test-token"
    publisher._destination_chat_id = -100123
    publisher._record_summary_success = Mock()
    publisher._record_summary_failure = Mock()
    api_call = Mock(return_value={"message_id": 1200})
    monkeypatch.setattr("app.telegram_publisher_day34._bot_api_call", api_call)
    attempt = SummaryPublicationAttempt(uuid4(), uuid4(), "Latest P&L", edit_message_id=1200)

    await publisher._deliver(attempt)

    assert api_call.call_args.args[1] == "editMessageText"
    assert api_call.call_args.args[2]["message_id"] == 1200
    publisher._record_summary_success.assert_called_once_with(attempt, 1200)
    publisher._record_summary_failure.assert_not_called()
