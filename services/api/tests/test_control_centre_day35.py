from app.control_centre_day35 import Day35ControlCentreService


def _metrics(**overrides: int) -> dict[str, int]:
    values = {
        "mt5_attention": 0,
        "publication_failed": 0,
        "push_failed_24h": 0,
        "telegram_notification_failed_24h": 0,
        "message_errors_24h": 0,
        "review_open": 0,
        "source_paused": 0,
    }
    values.update(overrides)
    return values


def test_review_backlog_is_attention_not_false_system_failure() -> None:
    items = Day35ControlCentreService._attention(
        _metrics(review_open=236),
        live_board_ready=True,
        live_board_pinned=True,
    )

    assert len(items) == 1
    assert items[0].key == "review"
    assert items[0].tone == "attention"
    assert items[0].count == 236
    assert Day35ControlCentreService._overall_status(items) == "attention"


def test_delivery_failure_is_critical_but_does_not_claim_trade_action() -> None:
    items = Day35ControlCentreService._attention(
        _metrics(publication_failed=2, push_failed_24h=1),
        live_board_ready=True,
        live_board_pinned=True,
    )

    delivery = next(item for item in items if item.key == "delivery")
    assert delivery.tone == "critical"
    assert delivery.count == 3
    assert Day35ControlCentreService._overall_status(items) == "critical"


def test_clean_snapshot_reports_healthy() -> None:
    items = Day35ControlCentreService._attention(
        _metrics(),
        live_board_ready=True,
        live_board_pinned=True,
    )

    assert len(items) == 1
    assert items[0].key == "healthy"
    assert items[0].tone == "healthy"
    assert Day35ControlCentreService._overall_status(items) == "healthy"


def test_unpinned_live_board_is_operator_attention() -> None:
    items = Day35ControlCentreService._attention(
        _metrics(),
        live_board_ready=True,
        live_board_pinned=False,
    )

    board = next(item for item in items if item.key == "live-board")
    assert board.tone == "attention"
    assert Day35ControlCentreService._overall_status(items) == "attention"
