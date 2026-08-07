"""Short-lived Telegram QR rendering checks."""

from __future__ import annotations

import base64

import pytest

from app.telegram_qr import qr_svg_data_uri


def test_telegram_login_payload_renders_as_self_contained_svg() -> None:
    payload = "tg://login?token=private-test-token"

    data_uri = qr_svg_data_uri(payload)

    prefix = "data:image/svg+xml;base64,"
    assert data_uri.startswith(prefix)
    svg = base64.b64decode(data_uri.removeprefix(prefix))
    assert b"<svg" in svg
    assert b"<path" in svg
    assert payload.encode("utf-8") not in svg


def test_non_telegram_payload_is_rejected() -> None:
    with pytest.raises(ValueError, match="invalid QR login payload"):
        qr_svg_data_uri("https://example.com/not-telegram")
