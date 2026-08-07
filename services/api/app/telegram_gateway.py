"""Telegram authorisation gateway backed by Telethon."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol
from uuid import UUID

from telethon import TelegramClient
from telethon.errors import (
    AuthTokenExpiredError,
    AuthTokenInvalidError,
    PasswordHashInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession


class TelegramGatewayError(RuntimeError):
    """Base error for Telegram gateway failures."""


class TelegramFlowNotFoundError(TelegramGatewayError):
    """Raised when an authorisation flow is missing or has already ended."""


class TelegramPasswordInvalidError(TelegramGatewayError):
    """Raised when Telegram rejects the account's two-step verification password."""


class TelegramSessionInvalidError(TelegramGatewayError):
    """Raised when a stored Telegram session is no longer authorised."""


@dataclass(frozen=True, slots=True)
class TelegramIdentity:
    user_id: int
    phone_number_e164: str
    username: str | None


@dataclass(frozen=True, slots=True)
class TelegramQrAuthorization:
    flow_id: UUID
    qr_url: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class TelegramAuthorizationResult:
    status: Literal["pending", "password_required", "connected", "expired"]
    session_string: str | None = None
    identity: TelegramIdentity | None = None


class TelegramGateway(Protocol):
    async def begin_qr_authorization(self, flow_id: UUID) -> TelegramQrAuthorization: ...

    async def poll_qr_authorization(
        self, flow_id: UUID
    ) -> TelegramAuthorizationResult: ...

    async def submit_password(
        self, flow_id: UUID, password: str
    ) -> TelegramAuthorizationResult: ...

    async def check_session(self, session_string: str) -> TelegramIdentity: ...

    async def revoke_session(self, session_string: str) -> None: ...

    async def discard_flow(self, flow_id: UUID) -> None: ...


@dataclass(slots=True)
class _ActiveQrFlow:
    client: TelegramClient
    wait_task: asyncio.Task[object]
    expires_at: datetime
    expiry_task: asyncio.Task[None] | None = None
    password_required: bool = False


class TelethonTelegramGateway:
    """Run short-lived QR flows and export portable Telethon StringSessions."""

    def __init__(self, api_id: int, api_hash: str, qr_ttl_seconds: int = 120) -> None:
        if qr_ttl_seconds <= 0:
            raise ValueError("Telegram QR TTL must be positive.")
        self._api_id = api_id
        self._api_hash = api_hash
        self._qr_ttl_seconds = qr_ttl_seconds
        self._flows: dict[UUID, _ActiveQrFlow] = {}

    def _client(self, session_string: str = "") -> TelegramClient:
        client = TelegramClient(
            StringSession(session_string),
            self._api_id,
            self._api_hash,
            device_model="Super Signals Server",
            app_version="0.4",
            system_lang_code="en",
            lang_code="en",
        )
        client.session.save_entities = False
        return client

    async def begin_qr_authorization(self, flow_id: UUID) -> TelegramQrAuthorization:
        if flow_id in self._flows:
            raise TelegramGatewayError("Telegram authorisation flow already exists.")

        client = self._client()
        try:
            await client.connect()
            qr_login = await client.qr_login()
            telegram_expiry = self._as_utc(qr_login.expires)
            configured_expiry = datetime.now(UTC) + timedelta(
                seconds=self._qr_ttl_seconds
            )
            expires_at = min(telegram_expiry, configured_expiry)
            timeout = max(1.0, (expires_at - datetime.now(UTC)).total_seconds())
            wait_task = asyncio.create_task(qr_login.wait(timeout=timeout))
            flow = _ActiveQrFlow(
                client=client,
                wait_task=wait_task,
                expires_at=expires_at,
            )
            self._flows[flow_id] = flow
            flow.expiry_task = asyncio.create_task(
                self._expire_flow(flow_id, expires_at)
            )
            return TelegramQrAuthorization(
                flow_id=flow_id,
                qr_url=qr_login.url,
                expires_at=expires_at,
            )
        except Exception:
            await client.disconnect()
            raise

    async def poll_qr_authorization(
        self, flow_id: UUID
    ) -> TelegramAuthorizationResult:
        flow = self._flows.get(flow_id)
        if flow is None:
            raise TelegramFlowNotFoundError("Telegram authorisation flow was not found.")

        if flow.password_required:
            return TelegramAuthorizationResult(status="password_required")

        if not flow.wait_task.done():
            if datetime.now(UTC) >= flow.expires_at:
                await self.discard_flow(flow_id)
                return TelegramAuthorizationResult(status="expired")
            return TelegramAuthorizationResult(status="pending")

        try:
            flow.wait_task.result()
        except SessionPasswordNeededError:
            flow.password_required = True
            return TelegramAuthorizationResult(status="password_required")
        except (TimeoutError, AuthTokenExpiredError, AuthTokenInvalidError):
            await self.discard_flow(flow_id)
            return TelegramAuthorizationResult(status="expired")
        except Exception as exc:
            await self.discard_flow(flow_id)
            raise TelegramGatewayError("Telegram QR authorisation failed.") from exc

        return await self._finalize(flow_id)

    async def submit_password(
        self, flow_id: UUID, password: str
    ) -> TelegramAuthorizationResult:
        flow = self._flows.get(flow_id)
        if flow is None:
            raise TelegramFlowNotFoundError("Telegram authorisation flow was not found.")
        if not flow.password_required:
            raise TelegramGatewayError(
                "Telegram has not requested a two-step verification password."
            )

        try:
            await flow.client.sign_in(password=password)
        except PasswordHashInvalidError as exc:
            raise TelegramPasswordInvalidError(
                "Telegram rejected the two-step verification password."
            ) from exc
        except Exception as exc:
            raise TelegramGatewayError(
                "Telegram two-step verification could not be completed."
            ) from exc

        return await self._finalize(flow_id)

    async def _finalize(self, flow_id: UUID) -> TelegramAuthorizationResult:
        flow = self._flows.get(flow_id)
        if flow is None:
            raise TelegramFlowNotFoundError("Telegram authorisation flow was not found.")

        try:
            if not await flow.client.is_user_authorized():
                raise TelegramSessionInvalidError(
                    "Telegram did not authorise the new session."
                )
            identity = await self._identity(flow.client)
            session_string = flow.client.session.save()
            if not session_string:
                raise TelegramGatewayError("Telethon returned an empty session.")
            return TelegramAuthorizationResult(
                status="connected",
                session_string=session_string,
                identity=identity,
            )
        finally:
            self._flows.pop(flow_id, None)
            await self._cancel_task(flow.expiry_task)
            await flow.client.disconnect()

    async def check_session(self, session_string: str) -> TelegramIdentity:
        client = self._client(session_string)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise TelegramSessionInvalidError(
                    "The stored Telegram session is no longer authorised."
                )
            return await self._identity(client)
        finally:
            await client.disconnect()

    async def revoke_session(self, session_string: str) -> None:
        client = self._client(session_string)
        try:
            await client.connect()
            if await client.is_user_authorized():
                logged_out = await client.log_out()
                if not logged_out:
                    raise TelegramGatewayError(
                        "Telegram did not confirm the remote logout."
                    )
        finally:
            if client.is_connected():
                await client.disconnect()

    async def discard_flow(self, flow_id: UUID) -> None:
        flow = self._flows.pop(flow_id, None)
        if flow is None:
            return
        await self._cancel_task(flow.wait_task)
        await self._cancel_task(flow.expiry_task)
        await flow.client.disconnect()

    async def _expire_flow(self, flow_id: UUID, expires_at: datetime) -> None:
        cleanup_at = expires_at + timedelta(seconds=30)
        delay = max(0.0, (cleanup_at - datetime.now(UTC)).total_seconds())
        await asyncio.sleep(delay)
        await self.discard_flow(flow_id)

    @staticmethod
    async def _cancel_task(task: asyncio.Task[object] | None) -> None:
        if task is None or task is asyncio.current_task():
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    async def _identity(client: TelegramClient) -> TelegramIdentity:
        me = await client.get_me()
        if me is None or me.id is None:
            raise TelegramGatewayError("Telegram did not return an account identity.")
        phone = (me.phone or "").strip()
        if not phone:
            raise TelegramGatewayError(
                "Telegram did not return a phone number for this user account."
            )
        phone_e164 = phone if phone.startswith("+") else f"+{phone}"
        return TelegramIdentity(
            user_id=int(me.id),
            phone_number_e164=phone_e164,
            username=me.username,
        )
