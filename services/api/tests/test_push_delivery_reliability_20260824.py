from __future__ import annotations

from uuid import uuid4

from app import push_notifications_day34
from app.push_notifications_day34 import Day34PushNotificationManager


def test_trade_push_is_retained_for_sleeping_device_and_sent_high_urgency(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_webpush(**kwargs) -> None:
        captured.update(kwargs)

    manager = Day34PushNotificationManager(
        session_factory=None,  # type: ignore[arg-type]
        vapid_private_key="test-private-key",
        vapid_subject="mailto:test@example.com",
    )
    monkeypatch.setattr(push_notifications_day34, "webpush", fake_webpush)
    monkeypatch.setattr(manager, "_record_success", lambda delivery_id, subscription_id: None)

    outcome = manager._send_claimed(
        {
            "delivery_id": uuid4(),
            "subscription_id": uuid4(),
            "notification_id": uuid4(),
            "kind": "trade_open",
            "title": "Trade opened",
            "body": "XAUUSD BUY opened",
            "endpoint": "https://push.example.test/subscription",
            "p256dh": "test-p256dh",
            "auth": "test-auth",
        }
    )

    assert outcome == "sent"
    assert captured["ttl"] == 24 * 60 * 60
    assert captured["headers"] == {"Urgency": "high"}
