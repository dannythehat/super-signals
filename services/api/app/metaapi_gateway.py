"""Provisioning-only MetaAPI gateway for Day 22.

This module intentionally exposes account creation, deployment and connection
status only. It contains no trading/order endpoint.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from uuid import uuid4

import httpx

DEFAULT_METAAPI_PROVISIONING_URL = (
    "https://mt-provisioning-api-v1.agiliumtrade.agiliumtrade.ai"
)
SUPER_SIGNALS_MAGIC = 220022
_SAFE_REMOTE_CODE = re.compile(r"^[A-Z0-9_\-]{1,64}$")


class MetaApiGatewayError(RuntimeError):
    """Sanitized MetaAPI failure which is safe to persist and return."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
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


class MetaApiProvisioningGateway:
    """Small REST client limited to MetaAPI's MT account management API."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout_seconds: float = 90.0,
    ) -> None:
        self._base_url = (
            base_url
            or os.getenv("SUPER_SIGNALS_METAAPI_PROVISIONING_URL")
            or DEFAULT_METAAPI_PROVISIONING_URL
        ).rstrip("/")
        self._timeout = httpx.Timeout(timeout_seconds)

    @staticmethod
    def new_transaction_id() -> str:
        return uuid4().hex

    async def find_account(
        self,
        *,
        token: str,
        login: str,
        server: str,
    ) -> MetaApiAccountState | None:
        response = await self._request(
            "GET",
            "/users/current/accounts",
            token=token,
            params=[("version", "5")],
        )
        payload = self._json(response)
        rows = payload.get("items", []) if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise MetaApiGatewayError("metaapi_invalid_response")
        for item in rows:
            if not isinstance(item, dict):
                continue
            if str(item.get("login", "")) != login:
                continue
            if str(item.get("server", "")).casefold() != server.casefold():
                continue
            return self._account_state(item)
        return None

    async def create_account(
        self,
        *,
        token: str,
        login: str,
        password: str,
        server: str,
        transaction_id: str,
    ) -> MetaApiCreateResult:
        response = await self._request(
            "POST",
            "/users/current/accounts",
            token=token,
            transaction_id=transaction_id,
            json={
                "login": login,
                "password": password,
                "name": "Super Signals owner demo",
                "server": server,
                "platform": "mt5",
                "magic": SUPER_SIGNALS_MAGIC,
                "type": "cloud-g2",
                "manualTrades": False,
            },
            accepted_statuses={201, 202},
        )
        if response.status_code == 202:
            return MetaApiCreateResult(
                pending=True,
                retry_after_seconds=self._retry_after_seconds(response),
            )
        payload = self._json(response)
        if not isinstance(payload, dict) or not payload.get("id"):
            raise MetaApiGatewayError("metaapi_invalid_response")
        return MetaApiCreateResult(
            pending=False,
            account_id=str(payload["id"]),
            state=str(payload.get("state") or "UNKNOWN").upper(),
        )

    async def read_account(
        self,
        *,
        token: str,
        account_id: str,
    ) -> MetaApiAccountState:
        response = await self._request(
            "GET",
            f"/users/current/accounts/{account_id}",
            token=token,
        )
        payload = self._json(response)
        if not isinstance(payload, dict):
            raise MetaApiGatewayError("metaapi_invalid_response")
        return self._account_state(payload, fallback_account_id=account_id)

    async def deploy_account(self, *, token: str, account_id: str) -> None:
        await self._request(
            "POST",
            f"/users/current/accounts/{account_id}/deploy",
            token=token,
            accepted_statuses={200, 201, 202, 204},
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        transaction_id: str | None = None,
        params: list[tuple[str, str]] | None = None,
        json: dict[str, object] | None = None,
        accepted_statuses: set[int] | None = None,
    ) -> httpx.Response:
        headers = {"Accept": "application/json", "auth-token": token}
        if json is not None:
            headers["Content-Type"] = "application/json"
        if transaction_id is not None:
            headers["transaction-id"] = transaction_id
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.request(
                    method,
                    f"{self._base_url}{path}",
                    headers=headers,
                    params=params,
                    json=json,
                )
        except httpx.TimeoutException as exc:
            raise MetaApiGatewayError("metaapi_timeout", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise MetaApiGatewayError("metaapi_unreachable", retryable=True) from exc

        allowed = accepted_statuses or {200}
        if response.status_code in allowed:
            return response
        raise self._error_from_response(response)

    @staticmethod
    def _json(response: httpx.Response) -> object:
        try:
            return response.json()
        except ValueError as exc:
            raise MetaApiGatewayError("metaapi_invalid_response") from exc

    @staticmethod
    def _error_from_response(response: httpx.Response) -> MetaApiGatewayError:
        if response.status_code == 401:
            return MetaApiGatewayError("metaapi_token_invalid")
        if response.status_code == 403:
            return MetaApiGatewayError("metaapi_permission_denied")
        if response.status_code == 404:
            return MetaApiGatewayError("metaapi_account_not_found")
        if response.status_code in {408, 425, 429} or response.status_code >= 500:
            return MetaApiGatewayError(
                "metaapi_temporarily_unavailable",
                retryable=True,
            )

        remote_code: str | None = None
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            details = body.get("details")
            if isinstance(details, str) and _SAFE_REMOTE_CODE.fullmatch(details):
                remote_code = details
            elif isinstance(details, dict):
                candidate = details.get("code")
                if isinstance(candidate, str) and _SAFE_REMOTE_CODE.fullmatch(candidate):
                    remote_code = candidate
            if remote_code is None:
                candidate = body.get("error")
                if isinstance(candidate, str) and _SAFE_REMOTE_CODE.fullmatch(candidate):
                    remote_code = candidate
        if remote_code:
            return MetaApiGatewayError(
                f"metaapi_{remote_code.lower().replace('-', '_')}"
            )
        return MetaApiGatewayError("metaapi_account_rejected")

    @staticmethod
    def _account_state(
        payload: dict[str, object],
        *,
        fallback_account_id: str | None = None,
    ) -> MetaApiAccountState:
        account_id = payload.get("_id") or payload.get("id") or fallback_account_id
        if not account_id:
            raise MetaApiGatewayError("metaapi_invalid_response")
        return MetaApiAccountState(
            account_id=str(account_id),
            login=str(payload.get("login") or ""),
            server=str(payload.get("server") or ""),
            state=str(payload.get("state") or "UNKNOWN").upper(),
            connection_status=str(
                payload.get("connectionStatus") or "DISCONNECTED"
            ).upper(),
        )

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> int:
        raw = response.headers.get("retry-after")
        if not raw:
            return 3
        try:
            seconds = int(raw)
            return max(1, min(seconds, 90))
        except ValueError:
            pass
        try:
            target = parsedate_to_datetime(raw)
            if target.tzinfo is None:
                target = target.replace(tzinfo=UTC)
            seconds = int(
                (target.astimezone(UTC) - datetime.now(UTC)).total_seconds()
            )
            return max(1, min(seconds, 90))
        except (TypeError, ValueError, OverflowError):
            return 3
