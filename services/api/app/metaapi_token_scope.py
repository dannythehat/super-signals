"""Local-only inspection of MetaAPI JWT permission scope.

No signature or secret value is logged or persisted here. The JWT payload is only
used to detect whether a narrowed token lacks terminal-data application access.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

TERMINAL_APPLICATIONS = frozenset(
    {
        "metaapi-api",
        "metaapi-rest-api",
        "metaapi-rpc-api",
        "metaapi-real-time-streaming-api",
    }
)


@dataclass(frozen=True, slots=True)
class MetaApiTokenScope:
    jwt_payload_decoded: bool
    access_rule_ids: tuple[str, ...]
    applications: tuple[str, ...]
    roles: tuple[str, ...]
    resource_rules_present: bool

    @property
    def is_explicitly_narrowed(self) -> bool:
        return self.resource_rules_present or bool(self.access_rule_ids or self.applications)

    @property
    def has_terminal_access(self) -> bool:
        names = set(self.access_rule_ids) | set(self.applications)
        return bool(names & TERMINAL_APPLICATIONS)


def inspect_metaapi_token_scope(token: str) -> MetaApiTokenScope:
    claims = _decode_jwt_payload(token)
    if claims is None:
        return MetaApiTokenScope(False, (), (), (), False)

    applications: set[str] = set()
    roles: set[str] = set()
    access_rule_ids: set[str] = set()
    resource_rules_present = False

    def walk(value: Any, key: str | None = None) -> None:
        nonlocal resource_rules_present
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                normalized = str(child_key)
                if normalized in {"resource", "resources"}:
                    resource_rules_present = True
                walk(child_value, normalized)
            return
        if isinstance(value, list):
            for item in value:
                walk(item, key)
            return
        if not isinstance(value, str):
            return
        if key in {"application", "applications"} and len(value) <= 128:
            applications.add(value)
        elif key in {"role", "roles"} and len(value) <= 64:
            roles.add(value)

    walk(claims)
    access_rules = claims.get("accessRules")
    if isinstance(access_rules, list):
        for rule in access_rules:
            if not isinstance(rule, dict):
                continue
            rule_id = rule.get("id")
            application = rule.get("application")
            if isinstance(rule_id, str) and len(rule_id) <= 128:
                access_rule_ids.add(rule_id)
            if isinstance(application, str) and len(application) <= 128:
                applications.add(application)

    return MetaApiTokenScope(
        jwt_payload_decoded=True,
        access_rule_ids=tuple(sorted(access_rule_ids)),
        applications=tuple(sorted(applications)),
        roles=tuple(sorted(roles)),
        resource_rules_present=resource_rules_present,
    )


def _decode_jwt_payload(token: str) -> dict[str, Any] | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        value = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None
