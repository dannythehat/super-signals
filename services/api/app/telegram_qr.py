"""Render short-lived Telegram login tokens as self-contained QR images."""

from __future__ import annotations

import base64
from io import BytesIO

import qrcode
from qrcode.image.svg import SvgPathImage


def qr_svg_data_uri(payload: str) -> str:
    """Return an SVG data URI without persisting or logging the QR payload."""
    if not payload.startswith("tg://login?"):
        raise ValueError("Telegram returned an invalid QR login payload.")

    image = qrcode.make(
        payload,
        image_factory=SvgPathImage,
        box_size=8,
        border=4,
    )
    output = BytesIO()
    image.save(output)
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"
