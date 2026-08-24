"""Versioned, evidence-derived language profiles for Telegram signal providers.

Profiles describe communication grammar only. They never supply prices, protection,
position size or a management action absent from the current message. The deterministic
execution policy remains authoritative.
"""

from __future__ import annotations

from typing import Any

PROFILE_VERSION = "2026-08-24.1"

PROVIDER_PROFILES: dict[str, dict[str, Any]] = {
    "the gold club - tgc": {
        "id": "tgc_xauusd",
        "instrument": "XAUUSD",
        "entry_style": "zones and terse present-tense entries; may misspell SELLING",
        "edit_style": "message edits can complete or correct a setup",
        "management_style": "TP/SL results and explicit protective commands",
    },
    "tdc v2 💎 (new)": {
        "id": "tdc_xauusd",
        "instrument": "XAUUSD",
        "entry_style": "zones, layered grids and runners",
        "edit_style": "frequently completes a signal by editing the original post",
        "management_style": "layer-specific closes, breakeven and runner management",
    },
    "tig’s asia trades": {
        "id": "tig_xauusd",
        "instrument": "XAUUSD",
        "entry_style": "short BUY/SELL GOLD NOW trigger followed by a structured same-message edit",
        "edit_style": "ENTRY plus Second entry, SL, numeric TPs and optional TP OPEN activates once",
        "management_style": "result posts are evidence unless an explicit command is present",
    },
    "fxtradingvision l forex & crypto signals 🚀": {
        "id": "fxtradingvision",
        "instrument": None,
        "entry_style": "mostly exact entries with numeric SL and TPs",
        "edit_style": "some corrections; current revision is authoritative",
        "management_style": "explicit close/protection wording only",
    },
    "gtmo vip 🤴🏽": {
        "id": "gtmo",
        "instrument": None,
        "entry_style": "mostly entry zones, commonly with a runner",
        "edit_style": "occasional completion or correction",
        "management_style": "preparation/precursor posts are not entries; explicit activation is",
    },
    "united kings™ signals! 👑": {
        "id": "united_kings",
        "instrument": None,
        "entry_style": "two-boundary zones with usually two numeric targets",
        "edit_style": "current complete revision is authoritative",
        "management_style": "results alone do not mutate broker state",
    },
    "sureshot gold": {
        "id": "sureshot_xauusd",
        "instrument": "XAUUSD",
        "entry_style": "mostly exact Gold entries with a single numeric target",
        "edit_style": "may complete an earlier incomplete post",
        "management_style": "explicit management commands only",
    },
    "pipxpert - forex signals": {
        "id": "pipxpert",
        "instrument": None,
        "entry_style": "forex-style exact or bounded entries; instrument must be literal",
        "edit_style": "current revision is authoritative",
        "management_style": "explicit management commands only",
    },
    "matthew trades": {
        "id": "matthew_xauusd",
        "instrument": "XAUUSD",
        "entry_style": "PREPARE is inert; BUY/SELL LIMITS GOLD @ A/B AREA is a pending grid; BUY/SELL GOLD @ A/B is market first plus layered limits",
        "edit_style": "full current setup is authoritative",
        "management_style": "close N layers, leave best entry running, price-specific layer closes, risk-free/breakeven and extended runner targets",
        "safety": "paper-enabled only; HIGH RISK is descriptive and never changes configured risk",
    },
}


def provider_profile(source_name: str | None) -> dict[str, Any] | None:
    profile = PROVIDER_PROFILES.get(str(source_name or "").strip().lower())
    if profile is None:
        return None
    return {"version": PROFILE_VERSION, **profile}


def execution_profile_id(source_name: str | None) -> str | None:
    profile = provider_profile(source_name)
    if profile is None:
        return None
    return str(profile["id"])


__all__ = ["PROFILE_VERSION", "PROVIDER_PROFILES", "execution_profile_id", "provider_profile"]
