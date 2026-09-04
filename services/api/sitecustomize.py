"""Post-incident compatibility shim for 4 September 2026.

The broker cleanup/reporting quarantine used during the morning Asia incident is retired.
After the 11:00 Europe/Sofia clean restart it must never mutate broker exposure or replace
the restart reporting boundary again.

The provider-language compatibility patch remains active until those phrases are folded
into the canonical management policy. It recognises the explicit lifecycle instructions
that triggered the incident without performing any startup broker/reporting mutation.
"""

from __future__ import annotations

import os
import re

_INCIDENT_ENV = "SUPER_SIGNALS_ASIA_INCIDENT_20260904"


def _runtime_enabled() -> bool:
    return (
        os.getenv(_INCIDENT_ENV, "").strip() == "1"
        and os.getcwd() == "/app"
        and "/app/services/api" in os.getenv("PYTHONPATH", "")
    )


def _install_reader_hotfix() -> None:
    """Patch explicit provider lifecycle wording without mutating broker/reporting state."""
    from app import day27_management_policy as policy

    current = policy.extract_day27_management_actions
    if getattr(current, "_ss_sep4_reader_hotfix", False):
        return

    result_type = policy.Day27ManagementPolicyResult
    out_at_be = re.compile(
        r"^\s*OUT\s+AT\s+(?:BE|BREAKEVEN|BREAK\s+EVEN)\b",
        re.IGNORECASE | re.DOTALL,
    )
    close_fully = re.compile(
        r"\bCLOSE\s+FULLY\b(?:\s+(?:WITH|AT|FOR)\s+[+-]?\d+(?:\.\d+)?\s*PIPS?\b)?",
        re.IGNORECASE,
    )
    book_some_profits = re.compile(
        r"\bBOOK\s+(?:SOME\s+)?PROFITS?\b",
        re.IGNORECASE,
    )
    explicit_partial = re.compile(
        r"\b(?:BOOK|TAKE)\s+PARTIAL\b|\bTP\s*\d+\b.*\bHIT\b",
        re.IGNORECASE | re.DOTALL,
    )

    def incident_extract(raw_text: str):
        text_value = (raw_text or "").strip()
        if out_at_be.search(text_value):
            return result_type(
                ({"type": "close", "target": "all", "value": None},),
                "incident_provider_exit_out_at_be",
            )
        if close_fully.search(text_value):
            return result_type(
                ({"type": "close", "target": "all", "value": None},),
                "incident_provider_explicit_close_fully",
            )
        if book_some_profits.search(text_value) and not explicit_partial.search(text_value):
            return result_type(
                ({"type": "close", "target": "profitable_only", "value": None},),
                "incident_profit_qualified_close",
            )
        return current(raw_text)

    incident_extract._ss_sep4_reader_hotfix = True  # type: ignore[attr-defined]
    policy.extract_day27_management_actions = incident_extract
    print("SUPER_SIGNALS_ASIA_READER_HOTFIX=ACTIVE", flush=True)


def _retire_legacy_owner_cleanup() -> None:
    """Make the pre-restart one-shot owner cleanup inert on every later process start."""
    try:
        from app import incident_20260904_asia_owner_cleanup as legacy
    except Exception:
        return

    async def retired() -> None:
        print("SUPER_SIGNALS_ASIA_OWNER_CLEANUP=RETIRED_AFTER_1100_RESTART", flush=True)

    legacy.run_owner_asia_cleanup = retired


if _runtime_enabled():
    try:
        _install_reader_hotfix()
        _retire_legacy_owner_cleanup()
        print("SUPER_SIGNALS_ASIA_STARTUP_CLEANUP=RETIRED", flush=True)
    except Exception as exc:
        print(
            f"SUPER_SIGNALS_ASIA_READER_HOTFIX_FATAL={type(exc).__name__}:{str(exc)[:180]}",
            flush=True,
        )
