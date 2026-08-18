"""Bridge explicit active-trade add-market instructions into lifecycle execution.

The provider entry reliability parser can emit ``update_type=add_market`` for messages
such as ``OPEN EXTRA GOLD SELLS``. The canonical lifecycle bridge historically did not
render that update type, so the otherwise-valid decision stopped at
``provider_update_unsupported`` and Day 28 later reported
``day28_lifecycle_event_not_resolved``.

This patch only makes that already-explicit management action representable in the
lifecycle ledger. The paper-only Day 27 execution override remains responsible for all
broker safety checks, active-trade linkage, side matching, existing SL/TP preservation,
idempotency and rollback.
"""

from __future__ import annotations

from typing import Any


def install_add_market_lifecycle_override() -> None:
    from app.ai_lifecycle_bridge import AiLifecycleBridge

    original = AiLifecycleBridge._render
    if getattr(original, "_add_market_supported", False):
        return

    def render(update_type: str, extracted: dict[str, Any]) -> tuple[str | None, str]:
        if update_type == "add_market":
            side = str(extracted.get("update_value") or "").strip().upper()
            suffix = f" {side}" if side in {"BUY", "SELL"} else ""
            return "add_market", f"TRADE UPDATE\nAdditional{suffix} market entry instructed."
        return original(update_type, extracted)

    render._add_market_supported = True  # type: ignore[attr-defined]
    AiLifecycleBridge._render = staticmethod(render)


__all__ = ["install_add_market_lifecycle_override"]
