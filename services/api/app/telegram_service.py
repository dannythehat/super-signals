"""Secure Telegram connection orchestration and persistence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AuditEvent, TelegramAccount
from app.telegram_crypto import SessionDecryptionError, TelegramSessionCipher
from app.telegram_gateway import (
    TelegramAuthorizationResult,
    TelegramCodeAuthorization,
    TelegramCodeInvalidError,
    TelegramFlowNotFoundError,
    TelegramGateway,
    TelegramGatewayError,
    TelegramIdentity,
    TelegramPasswordInvalidError,
    TelegramPhoneInvalidError,
    TelegramQrAuthorization,
    TelegramSessionInvalidError,
    TelethonTelegramGateway,
)


class TelegramConfigurationError(RuntimeError):
    """Raised when the server is missing Telegram integration secrets."""


class TelegramConnectionNotFoundError(LookupError):
    """Raised when a flow or account does not belong to the signed-in admin."""


class TelegramConnectionConflictError(RuntimeError):
    """Raised when an encrypted Telegram session is already connected."""


@dataclass(frozen=True, slots=True)
class TelegramConnectionView:
    id: UUID
    label: str
    phone_hint: str
    status: str
    last_connected_at: datetime | None


@dataclass(frozen=True, slots=True)
class TelegramCodeAuthorizationView:
    flow_id: UUID
    phone_hint: str
    expires_at: datetime


class TelegramConnectionService:
    def __init__(
        self,
        gateway: TelegramGateway,
        cipher: TelegramSessionCipher,
    ) -> None:
        self._gateway = gateway
        self._cipher = cipher
        self._flow_owners: dict[UUID, UUID] = {}
        self._flow_labels: dict[UUID, str] = {}

    async def begin_authorization(
        self,
        session: Session,
        *,
        actor: dict[str, Any],
        label: str,
    ) -> TelegramQrAuthorization:
        cleaned_label = self._clean_label(label)
        flow_id = uuid4()
        authorization = await self._gateway.begin_qr_authorization(flow_id)
        self._remember_flow(flow_id, actor["id"], cleaned_label)
        self._audit(
            session,
            actor_id=actor["id"],
            event_type="telegram.authorization_started",
            entity_id=None,
            payload={
                "flow_id": str(flow_id),
                "label": cleaned_label,
                "method": "qr",
                "expires_at": authorization.expires_at.isoformat(),
            },
        )
        session.commit()
        return authorization

    async def begin_code_authorization(
        self,
        session: Session,
        *,
        actor: dict[str, Any],
        label: str,
        phone_number: str,
    ) -> TelegramCodeAuthorizationView:
        cleaned_label = self._clean_label(label)
        phone_number_e164 = self._normalize_phone(phone_number)
        flow_id = uuid4()
        authorization: TelegramCodeAuthorization = await self._gateway.begin_code_authorization(
            flow_id, phone_number_e164
        )
        self._remember_flow(flow_id, actor["id"], cleaned_label)
        phone_hint = self._mask_phone(phone_number_e164)
        self._audit(
            session,
            actor_id=actor["id"],
            event_type="telegram.code_authorization_started",
            entity_id=None,
            payload={
                "flow_id": str(flow_id),
                "label": cleaned_label,
                "method": "code",
                "phone_hint": phone_hint,
                "expires_at": authorization.expires_at.isoformat(),
            },
        )
        session.commit()
        return TelegramCodeAuthorizationView(
            flow_id=flow_id,
            phone_hint=phone_hint,
            expires_at=authorization.expires_at,
        )

    async def poll_authorization(
        self,
        session: Session,
        *,
        actor: dict[str, Any],
        flow_id: UUID,
    ) -> dict[str, Any]:
        self._assert_flow_owner(flow_id, actor["id"])
        result = await self._gateway.poll_qr_authorization(flow_id)
        return self._handle_authorization_result(
            session,
            actor=actor,
            flow_id=flow_id,
            result=result,
        )

    async def submit_code(
        self,
        session: Session,
        *,
        actor: dict[str, Any],
        flow_id: UUID,
        code: str,
    ) -> dict[str, Any]:
        self._assert_flow_owner(flow_id, actor["id"])
        result = await self._gateway.submit_code(flow_id, code)
        return self._handle_authorization_result(
            session,
            actor=actor,
            flow_id=flow_id,
            result=result,
        )

    async def submit_password(
        self,
        session: Session,
        *,
        actor: dict[str, Any],
        flow_id: UUID,
        password: str,
    ) -> dict[str, Any]:
        self._assert_flow_owner(flow_id, actor["id"])
        result = await self._gateway.submit_password(flow_id, password)
        return self._handle_authorization_result(
            session,
            actor=actor,
            flow_id=flow_id,
            result=result,
        )

    def list_accounts(
        self,
        session: Session,
        *,
        actor: dict[str, Any],
    ) -> list[TelegramConnectionView]:
        statement = select(TelegramAccount).order_by(TelegramAccount.created_at.asc())
        if actor["role"] != "owner":
            statement = statement.where(TelegramAccount.owner_user_id == actor["id"])
        accounts = session.scalars(statement).all()
        return [self._view(account) for account in accounts]

    async def verify_account(
        self,
        session: Session,
        *,
        actor: dict[str, Any],
        account_id: UUID,
    ) -> TelegramConnectionView:
        account = self._account_for_actor(session, actor, account_id)
        if account.status != "connected":
            raise TelegramSessionInvalidError(
                "This Telegram account has no active saved server session."
            )

        try:
            session_string = self._cipher.decrypt(account.session_ciphertext)
            telegram_identity = await self._gateway.check_session(session_string)
        except (SessionDecryptionError, TelegramSessionInvalidError):
            account.status = "revoked"
            self._destroy_server_session(account)
            self._audit(
                session,
                actor_id=actor["id"],
                event_type="telegram.session_invalid",
                entity_id=account.id,
                payload={"label": account.label, "server_session_destroyed": True},
            )
            session.commit()
            raise

        if telegram_identity.phone_number_e164 != account.phone_number_e164:
            remote_logout = True
            try:
                await self._gateway.revoke_session(session_string)
            except TelegramGatewayError:
                remote_logout = False
            account.status = "revoked"
            self._destroy_server_session(account)
            self._audit(
                session,
                actor_id=actor["id"],
                event_type="telegram.identity_mismatch",
                entity_id=account.id,
                payload={
                    "label": account.label,
                    "remote_logout": remote_logout,
                    "server_session_destroyed": True,
                },
            )
            session.commit()
            raise TelegramSessionInvalidError(
                "The stored Telegram session no longer matches the approved account."
            )

        account.status = "connected"
        account.last_connected_at = datetime.now(UTC)
        self._audit(
            session,
            actor_id=actor["id"],
            event_type="telegram.session_verified",
            entity_id=account.id,
            payload={"label": account.label},
        )
        session.commit()
        session.refresh(account)
        return self._view(account)

    async def disconnect_account(
        self,
        session: Session,
        *,
        actor: dict[str, Any],
        account_id: UUID,
    ) -> dict[str, Any]:
        account = self._account_for_actor(session, actor, account_id)
        remote_logout = False
        if account.status == "connected":
            try:
                session_string = self._cipher.decrypt(account.session_ciphertext)
                await self._gateway.revoke_session(session_string)
                remote_logout = True
            except (SessionDecryptionError, TelegramGatewayError):
                remote_logout = False

        self._destroy_server_session(account)
        account.status = "disconnected"
        self._audit(
            session,
            actor_id=actor["id"],
            event_type="telegram.account_disconnected",
            entity_id=account.id,
            payload={
                "label": account.label,
                "phone_hint": self._mask_phone(account.phone_number_e164),
                "remote_logout": remote_logout,
                "server_session_destroyed": True,
            },
        )
        session.commit()
        return {
            "disconnected": True,
            "server_session_destroyed": True,
            "remote_logout": remote_logout,
        }

    def _handle_authorization_result(
        self,
        session: Session,
        *,
        actor: dict[str, Any],
        flow_id: UUID,
        result: TelegramAuthorizationResult,
    ) -> dict[str, Any]:
        if result.status in {"pending", "password_required"}:
            return {"flow_id": flow_id, "status": result.status}
        if result.status == "expired":
            self._forget_flow(flow_id)
            return {"flow_id": flow_id, "status": "expired"}
        if result.session_string is None or result.identity is None:
            self._forget_flow(flow_id)
            raise TelegramGatewayError(
                "Telegram reported a connected flow without a portable session."
            )

        label = self._flow_labels.get(flow_id)
        if label is None:
            raise TelegramConnectionNotFoundError("Telegram authorisation flow was not found.")
        try:
            account = self._persist_connected_account(
                session,
                actor=actor,
                label=label,
                session_string=result.session_string,
                telegram_identity=result.identity,
            )
        finally:
            self._forget_flow(flow_id)
        return {
            "flow_id": flow_id,
            "status": "connected",
            "account": self._view(account),
        }

    def _persist_connected_account(
        self,
        session: Session,
        *,
        actor: dict[str, Any],
        label: str,
        session_string: str,
        telegram_identity: TelegramIdentity,
    ) -> TelegramAccount:
        fingerprint = self._cipher.fingerprint(session_string)
        account = session.scalar(
            select(TelegramAccount).where(
                TelegramAccount.owner_user_id == actor["id"],
                TelegramAccount.phone_number_e164 == telegram_identity.phone_number_e164,
            )
        )
        if account is None:
            account = TelegramAccount(
                owner_user_id=actor["id"],
                label=label,
                phone_number_e164=telegram_identity.phone_number_e164,
                session_ciphertext=self._cipher.encrypt(session_string),
                session_fingerprint=fingerprint,
                status="connected",
                last_connected_at=datetime.now(UTC),
            )
            session.add(account)
        else:
            account.label = label
            account.session_ciphertext = self._cipher.encrypt(session_string)
            account.session_fingerprint = fingerprint
            account.status = "connected"
            account.last_connected_at = datetime.now(UTC)

        try:
            session.flush()
        except IntegrityError as exc:
            session.rollback()
            raise TelegramConnectionConflictError(
                "This exact Telegram session is already connected."
            ) from exc

        self._audit(
            session,
            actor_id=actor["id"],
            event_type="telegram.account_connected",
            entity_id=account.id,
            payload={
                "label": label,
                "phone_hint": self._mask_phone(telegram_identity.phone_number_e164),
                "telegram_user_id": telegram_identity.user_id,
                "username_present": telegram_identity.username is not None,
            },
        )
        session.commit()
        session.refresh(account)
        return account

    def _destroy_server_session(self, account: TelegramAccount) -> None:
        marker = f"destroyed:{uuid4()}"
        account.session_ciphertext = self._cipher.encrypt(marker)
        account.session_fingerprint = self._cipher.fingerprint(marker)

    def _remember_flow(self, flow_id: UUID, actor_id: UUID, label: str) -> None:
        self._flow_owners[flow_id] = actor_id
        self._flow_labels[flow_id] = label

    def _assert_flow_owner(self, flow_id: UUID, actor_id: UUID) -> None:
        if self._flow_owners.get(flow_id) != actor_id:
            raise TelegramConnectionNotFoundError("Telegram authorisation flow was not found.")

    def _forget_flow(self, flow_id: UUID) -> None:
        self._flow_owners.pop(flow_id, None)
        self._flow_labels.pop(flow_id, None)

    @staticmethod
    def _clean_label(label: str) -> str:
        cleaned_label = label.strip()
        if not cleaned_label:
            raise ValueError("A Telegram account label is required.")
        return cleaned_label

    @staticmethod
    def _normalize_phone(phone_number: str) -> str:
        cleaned = re.sub(r"[\s().-]", "", phone_number.strip())
        if not re.fullmatch(r"\+[1-9]\d{6,14}", cleaned):
            raise ValueError(
                "Enter the Telegram phone number in international format, "
                "including + and country code."
            )
        return cleaned

    @staticmethod
    def _account_for_actor(
        session: Session,
        actor: dict[str, Any],
        account_id: UUID,
    ) -> TelegramAccount:
        statement = select(TelegramAccount).where(TelegramAccount.id == account_id)
        if actor["role"] != "owner":
            statement = statement.where(TelegramAccount.owner_user_id == actor["id"])
        account = session.scalar(statement)
        if account is None:
            raise TelegramConnectionNotFoundError("Telegram account was not found.")
        return account

    @staticmethod
    def _mask_phone(phone_number_e164: str) -> str:
        if len(phone_number_e164) <= 5:
            return "***"
        return f"{phone_number_e164[:3]}***{phone_number_e164[-3:]}"

    @classmethod
    def _view(cls, account: TelegramAccount) -> TelegramConnectionView:
        return TelegramConnectionView(
            id=account.id,
            label=account.label,
            phone_hint=cls._mask_phone(account.phone_number_e164),
            status=account.status,
            last_connected_at=account.last_connected_at,
        )

    @staticmethod
    def _audit(
        session: Session,
        *,
        actor_id: UUID,
        event_type: str,
        entity_id: UUID | None,
        payload: dict[str, Any],
    ) -> None:
        session.add(
            AuditEvent(
                actor_user_id=actor_id,
                event_type=event_type,
                entity_type="telegram_account",
                entity_id=entity_id,
                payload=payload,
            )
        )


@lru_cache
def get_telegram_connection_service() -> TelegramConnectionService:
    settings = get_settings()
    if settings.telegram_api_id is None or settings.telegram_api_hash is None:
        raise TelegramConfigurationError(
            "Telegram API credentials are not configured on the server."
        )
    return TelegramConnectionService(
        gateway=TelethonTelegramGateway(
            settings.telegram_api_id,
            settings.telegram_api_hash,
            settings.telegram_qr_ttl_seconds,
        ),
        cipher=TelegramSessionCipher(settings.telegram_session_keys),
    )


__all__ = [
    "SessionDecryptionError",
    "TelegramCodeInvalidError",
    "TelegramConfigurationError",
    "TelegramConnectionConflictError",
    "TelegramConnectionNotFoundError",
    "TelegramConnectionService",
    "TelegramConnectionView",
    "TelegramFlowNotFoundError",
    "TelegramGatewayError",
    "TelegramPasswordInvalidError",
    "TelegramPhoneInvalidError",
    "TelegramSessionInvalidError",
    "get_telegram_connection_service",
]
