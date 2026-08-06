"""Central role and permission definitions for Super Signals."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

ROLE_LABELS = {
    "owner": "Owner Admin",
    "trading_admin": "Trading Admin",
    "user": "Invited User",
}

ROLE_PRIORITY = ("owner", "trading_admin", "user")

PERMISSION_CATALOG: dict[str, tuple[str, str]] = {
    "profile.view": ("shared", "View the signed-in account and security status."),
    "users.manage": ("owner", "Invite, suspend and revoke platform users."),
    "access_keys.manage": ("owner", "Create and revoke personal access keys."),
    "admins.manage": ("owner", "Grant or remove administrator permissions."),
    "mt5_accounts.approve": ("owner", "Approve or alter connected MT5 accounts."),
    "security.manage": ("owner", "Change core platform security settings."),
    "settings.manage": ("owner", "Change protected system settings."),
    "environments.manage": ("owner", "Control testing and live environments."),
    "platform.delete": ("owner", "Delete the platform."),
    "sources.manage": ("trading", "Add, remove, pause and reactivate Telegram sources."),
    "sources.change_status": ("trading", "Move sources between Testing and Live."),
    "source_messages.view": ("trading", "View original Telegram source messages."),
    "signals.review": ("trading", "Review parsed signals and unclear messages."),
    "trades.review": ("trading", "Review trade execution and failures."),
    "activity.view": ("trading", "View user trading activity and system events."),
    "emergency_stop.use": ("trading", "Use the emergency trading stop."),
    "account.connect": ("user", "Connect one approved Vantage MT5 account."),
    "risk.manage": ("user", "Choose the allowed risk per position."),
    "automation.toggle": ("user", "Activate or stop automated trading."),
    "signals.view": ("user", "View approved signals without source identity."),
    "positions.view": ("user", "View positions and trade status."),
    "performance.view": ("user", "View trading performance."),
    "passkey.manage": ("user", "Set up device passkey security."),
}

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "owner": frozenset(PERMISSION_CATALOG),
    "trading_admin": frozenset(
        {
            "profile.view",
            "sources.manage",
            "sources.change_status",
            "source_messages.view",
            "signals.review",
            "trades.review",
            "activity.view",
            "emergency_stop.use",
        }
    ),
    "user": frozenset(
        {
            "profile.view",
            "account.connect",
            "risk.manage",
            "automation.toggle",
            "signals.view",
            "positions.view",
            "performance.view",
            "passkey.manage",
        }
    ),
}

SECTION_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "key": "owner",
        "label": "Owner controls",
        "description": "Users, access, approvals, security and protected platform settings.",
        "actions": (
            ("users.manage", "Invited users", "Invite, suspend or revoke users."),
            ("access_keys.manage", "Access keys", "Create and revoke personal access keys."),
            ("admins.manage", "Administrators", "Grant or remove administrator permissions."),
            ("mt5_accounts.approve", "MT5 approvals", "Approve connected trading accounts."),
            ("security.manage", "Security", "Control protected security settings."),
            ("environments.manage", "Environments", "Control testing and live environments."),
        ),
    },
    {
        "key": "trading",
        "label": "Trading operations",
        "description": "Telegram sources, review activity and emergency trading controls.",
        "actions": (
            ("sources.manage", "Signal sources", "Add, pause, resume or remove sources."),
            ("sources.change_status", "Testing and Live", "Move approved sources between environments."),
            ("source_messages.view", "Original messages", "Review private source messages."),
            ("signals.review", "Signal review", "Inspect parsing and unclear messages."),
            ("trades.review", "Trade execution", "Review fills, failures and outcomes."),
            ("activity.view", "System activity", "View trading activity and system events."),
            ("emergency_stop.use", "Emergency stop", "Stop automated trading in an emergency."),
        ),
    },
    {
        "key": "user",
        "label": "My trading",
        "description": "Your approved MT5 account, risk, automation and performance.",
        "actions": (
            ("account.connect", "MT5 account", "Connect one approved Vantage MT5 account."),
            ("risk.manage", "Risk setting", "Choose 0.5% or 1% risk per position."),
            ("automation.toggle", "Automated trading", "Activate or stop automated trading."),
            ("signals.view", "Signals", "View approved signals without provider details."),
            ("positions.view", "Positions", "View open and completed positions."),
            ("performance.view", "Performance", "View cash and percentage results."),
            ("passkey.manage", "Passkey", "Set up device passkey security."),
        ),
    },
)


def primary_role(roles: Iterable[str]) -> str:
    role_set = set(roles)
    for role in ROLE_PRIORITY:
        if role in role_set:
            return role
    raise ValueError("Account has no supported role.")


def build_access_sections(permissions: Iterable[str]) -> list[dict[str, Any]]:
    allowed = set(permissions)
    sections: list[dict[str, Any]] = []
    for section in SECTION_DEFINITIONS:
        actions = [
            {
                "permission": permission,
                "label": label,
                "description": description,
            }
            for permission, label, description in section["actions"]
            if permission in allowed
        ]
        if actions:
            sections.append(
                {
                    "key": section["key"],
                    "label": section["label"],
                    "description": section["description"],
                    "actions": actions,
                }
            )
    return sections
