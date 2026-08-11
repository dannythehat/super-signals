"""Anonymous visual identity for Super Signals Telegram posts."""

from __future__ import annotations

import hashlib

TESTING_BANNER = "✨🧪 TESTING 🧪✨\nDEMO / NOT LIVE\n\n"
SOURCE_MARKERS = ("🔵", "🟣", "🟢", "🟠", "🟡", "🔴")


def source_colour_marker(source_id: object) -> str:
    """Return a stable anonymous colour marker without exposing provider identity."""

    digest = hashlib.sha256(str(source_id).encode("utf-8")).digest()
    return SOURCE_MARKERS[digest[0] % len(SOURCE_MARKERS)]


def public_signal_reference(signal_id: object) -> str:
    """Return a short stable public reference for following one signal thread."""

    digest = hashlib.sha256(str(signal_id).encode("utf-8")).hexdigest().upper()
    return digest[:5]


def decorate_root_post(
    rendered_text: str,
    *,
    source_status: str,
    source_id: object,
    signal_id: object,
) -> str:
    marker = source_colour_marker(source_id)
    reference = public_signal_reference(signal_id)
    testing = TESTING_BANNER if source_status.strip().lower() == "testing" else ""
    return f"{testing}{marker} SIGNAL #{reference}\n\n{rendered_text}"


def decorate_lifecycle_post(
    rendered_text: str,
    *,
    source_status: str,
    source_id: object,
    signal_id: object,
) -> str:
    marker = source_colour_marker(source_id)
    reference = public_signal_reference(signal_id)
    testing = TESTING_BANNER if source_status.strip().lower() == "testing" else ""
    return f"{testing}{marker} #{reference}\n\n{rendered_text}"
