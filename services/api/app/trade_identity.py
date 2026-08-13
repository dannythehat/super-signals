"""Stable provider-hidden identity for a canonical Super Signals trade.

The canonical Signal UUID remains the database identity. This module derives a short
member-facing reference and a decorative marker from that UUID so the same trade is
recognisable across Telegram, the Live Trades Board, app timelines and admin views.
No provider, Telegram or account information is encoded in the public reference.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

_PUBLIC_MARKERS = ("🟣", "🟪", "🔷", "🟧", "🔶", "🔹", "🔸", "💠")


@dataclass(frozen=True, slots=True)
class PublicTradeIdentity:
    reference: str
    marker: str

    @property
    def label(self) -> str:
        return f"{self.marker} {self.reference}"


def public_trade_identity(signal_id: UUID | str) -> PublicTradeIdentity:
    """Return one deterministic public identity for the canonical Signal."""

    value = signal_id if isinstance(signal_id, UUID) else UUID(str(signal_id))
    compact = value.hex.upper()
    # Ten hexadecimal characters gives a short 40-bit visual reference. The full
    # canonical UUID remains authoritative internally; the marker is decorative only.
    reference = f"SS-{compact[:10]}"
    marker_index = int(compact[10:12], 16) % len(_PUBLIC_MARKERS)
    return PublicTradeIdentity(reference=reference, marker=_PUBLIC_MARKERS[marker_index])


def prefix_public_trade_identity(signal_id: UUID | str, text: str) -> str:
    """Prefix member-facing trade text without changing its status semantics."""

    identity = public_trade_identity(signal_id)
    clean = str(text).strip()
    return f"{identity.label} · {clean}" if clean else identity.label


__all__ = [
    "PublicTradeIdentity",
    "prefix_public_trade_identity",
    "public_trade_identity",
]
