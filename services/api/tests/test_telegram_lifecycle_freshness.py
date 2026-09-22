from pathlib import Path


def test_telegram_lifecycle_freshness_uses_materialization_time_only() -> None:
    source = Path("services/api/app/telegram_publisher_canonical.py").read_text()

    # Broker settlement can discover a legitimate close several minutes after the
    # broker's occurred_at timestamp. Freshness must therefore be based on when the
    # lifecycle row was materialised locally, otherwise TP/close/win posts disappear.
    assert "ev.created_at<:fresh_after" in source
    assert "WHERE ev.created_at>=:fresh_after" in source
    assert "ev.occurred_at<:fresh_after" not in source
    assert "ev.occurred_at>=:fresh_after" not in source
