"""Process-wide MetaAPI account-region cache.

MetaAPI terminal REST calls are region-scoped. Provisioning reconciliation already
reads each account's region before the Telegram listener starts, so execution should
reuse that known account metadata instead of making another provisioning request in
the trade-critical path.
"""

from __future__ import annotations

import re
from threading import Lock

_REGION = re.compile(r"^[a-z0-9-]{2,64}$")
_REGION_BY_ACCOUNT: dict[str, str] = {}
_LOCK = Lock()


def normalize_metaapi_region(region: str) -> str | None:
    normalized = region.strip().lower()
    return normalized if _REGION.fullmatch(normalized) else None


def remember_metaapi_region(account_id: str, region: str) -> str | None:
    account_key = account_id.strip()
    normalized = normalize_metaapi_region(region)
    if not account_key or normalized is None:
        return None
    with _LOCK:
        _REGION_BY_ACCOUNT[account_key] = normalized
    return normalized


def get_metaapi_region(account_id: str) -> str | None:
    account_key = account_id.strip()
    if not account_key:
        return None
    with _LOCK:
        return _REGION_BY_ACCOUNT.get(account_key)
