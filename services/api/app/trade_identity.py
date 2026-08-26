"""Simple, stable member-facing identity for one active Super Signals trade.

The canonical UUID remains the internal identity. Members see an explicit trade number
and one plain colour marker. A number stays fixed for the complete broker lifecycle and
can be reused only after that trade is fully closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

_MEMBER_COLOURS = ("🔵", "🟢", "🟣", "🟠", "🟡", "🔴", "🟤", "⚪")


@dataclass(frozen=True, slots=True)
class PublicTradeIdentity:
    reference: str
    marker: str

    @property
    def label(self) -> str:
        return f"{self.marker} {self.reference}"


def public_trade_identity(
    signal_id: UUID | str,
    trade_number: int | None = None,
) -> PublicTradeIdentity:
    """Return the stable member label, retaining a hidden-reference fallback."""

    value = signal_id if isinstance(signal_id, UUID) else UUID(str(signal_id))
    compact = value.hex.upper()
    if trade_number is not None:
        number = int(trade_number)
        if number < 1:
            raise ValueError("trade_number must be positive")
        return PublicTradeIdentity(
            reference=f"TRADE {number}",
            marker=_MEMBER_COLOURS[(number - 1) % len(_MEMBER_COLOURS)],
        )

    # Audit/backward-compatibility fallback only. The live member publisher assigns a
    # number before it makes a trade visible.
    return PublicTradeIdentity(
        reference=f"SS-{compact[:10]}",
        marker=_MEMBER_COLOURS[int(compact[10:12], 16) % len(_MEMBER_COLOURS)],
    )


def prefix_public_trade_identity(
    signal_id: UUID | str,
    text: str,
    trade_number: int | None = None,
) -> str:
    """Render roots, updates and final results in direct member language."""

    identity = public_trade_identity(signal_id, trade_number)
    clean = str(text).strip()
    if not clean:
        return identity.label
    if clean.startswith("TRADE UPDATE\n"):
        body = clean.removeprefix("TRADE UPDATE\n").strip()
        return f"{identity.label} UPDATE — {body}"
    first_line = clean.splitlines()[0]
    if "TRADE CLOSED" in first_line:
        return clean.replace("TRADE CLOSED", f"{identity.label} CLOSED", 1)
    return f"{identity.label} · {clean}"


__all__ = [
    "PublicTradeIdentity",
    "prefix_public_trade_identity",
    "public_trade_identity",
]
