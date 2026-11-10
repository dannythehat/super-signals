"""Fail-closed compatibility contracts for retired remote-broker interfaces.

The owner has removed the third-party broker API. This module deliberately
contains NO network transports, credentials, token storage, trade calls,
fallback trading, or external URLs. It only keeps legacy imports loadable
while historical routes are retired and the Contabo MT5 bridge is authoritative.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import uuid4


class MetaApiGatewayError(RuntimeError):
    def __init__(self, code: str = "retired_broker_api_disabled", *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class MetaApiAccountState:
    account_id: str
    login: str
    server: str
    state: str
    connection_status: str


@dataclass(frozen=True, slots=True)
class MetaApiCreateResult:
    pending: bool
    account_id: str | None = None
    state: str | None = None
    retry_after_seconds: int = 3


@dataclass(frozen=True, slots=True)
class MetaApiMarketOrderResult:
    order_id: str
    position_id: str | None
    numeric_code: int | None
    string_code: str


@dataclass(frozen=True, slots=True)
class MetaApiPendingOrderRequest:
    order_type: str
    symbol: str
    volume: float
    open_price: float
    stop_loss: float
    take_profit: float | None
    client_id: str


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
        return False


def inspect_metaapi_token_scope(token: str) -> MetaApiTokenScope:
    # The retired third-party API is never a valid connection option.
    return MetaApiTokenScope(False, (), (), (), False)


def normalize_metaapi_region(region: str) -> str | None:
    if not isinstance(region, str):
        return None
    value = region.strip().lower()
    return value if re.fullmatch(r"[a-z0-9-]{2,64}", value) else None


def remember_metaapi_region(account_id: str, region: str) -> None:
    # No legacy region or remote-account state may be reintroduced.
    return None


def get_metaapi_region(account_id: str) -> None:
    return None


class _RetiredRemoteBroker:
    """Never makes a call to any remote third-party broker service."""
    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def __getattr__(self, name: str):
        async def _blocked(*args: object, **kwargs: object):
            raise MetaApiGatewayError()
        if name.startswith("_") and name not in {"_request","_read_terminal_json","_trade_request","_current_protection"}:
            raise AttributeError(name)
        return _blocked

    @staticmethod
    async def _request(*args: object, **kwargs: object):
        raise MetaApiGatewayError()


class MetaApiReadGateway(_RetiredRemoteBroker):
    pass


class MetaApiTradeGateway(_RetiredRemoteBroker):
    pass


class MetaApiMarginGateway(_RetiredRemoteBroker):
    pass


class MetaApiProvisioningGateway(_RetiredRemoteBroker):
    @staticmethod
    def new_transaction_id() -> str:
        return str(uuid4())


class MetaApiPendingOrderGateway(_RetiredRemoteBroker):
    def __init__(self, base: object) -> None:
        self._base = base

    @staticmethod
    def _transport(base: object):
        raise MetaApiGatewayError()


__all__ = [
    "MetaApiGatewayError", "MetaApiAccountState", "MetaApiCreateResult",
    "MetaApiMarketOrderResult", "MetaApiPendingOrderRequest", "MetaApiTokenScope",
    "inspect_metaapi_token_scope", "normalize_metaapi_region",
    "remember_metaapi_region", "get_metaapi_region", "MetaApiReadGateway",
    "MetaApiTradeGateway", "MetaApiMarginGateway", "MetaApiProvisioningGateway",
    "MetaApiPendingOrderGateway",
]
