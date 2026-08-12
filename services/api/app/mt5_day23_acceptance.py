"""One-shot, opt-in Day 23 acceptance diagnostics.

The live probe is disabled by default and performs no trades. A scope-only mode
can inspect the encrypted stored MetaAPI token locally without making a MetaAPI
request; only safe permission metadata is persisted, never the token or resource ids.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.metaapi_read_gateway import MetaApiReadGateway
from app.models import AuditEvent
from app.mt5_crypto import BrokerCredentialDecryptionError, MetaApiTokenCipher
from app.mt5_read_service_day23 import Day23Mt5ReadService, Day23ReadError

logger = logging.getLogger(__name__)


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


def _safe_scope_names(
    claims: dict[str, Any],
) -> tuple[list[str], list[str], bool, list[str], list[str]]:
    applications: set[str] = set()
    roles: set[str] = set()
    rule_ids: set[str] = set()
    services: set[str] = set()
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
        if key in {"role", "roles"} and len(value) <= 64:
            roles.add(value)

    walk(claims)

    access_rules = claims.get("accessRules")
    if isinstance(access_rules, list):
        for rule in access_rules:
            if not isinstance(rule, dict):
                continue
            rule_id = rule.get("id")
            application = rule.get("application")
            service = rule.get("service")
            if isinstance(rule_id, str) and len(rule_id) <= 128:
                rule_ids.add(rule_id)
            if isinstance(application, str) and len(application) <= 128:
                applications.add(application)
            if isinstance(service, str) and len(service) <= 64:
                services.add(service)

    return (
        sorted(applications),
        sorted(roles),
        resource_rules_present,
        sorted(rule_ids),
        sorted(services),
    )


def _record_scope_only(
    *,
    session_factory: sessionmaker[Session],
    cipher: MetaApiTokenCipher,
    owner_user_id: UUID,
) -> None:
    with session_factory() as session:
        row = session.execute(
            text(
                """
                SELECT id, metaapi_token_ciphertext
                FROM mt5_accounts
                WHERE owner_user_id = :owner_user_id
                  AND status != 'revoked'
                LIMIT 1
                """
            ),
            {"owner_user_id": owner_user_id},
        ).mappings().first()

    if row is None:
        logger.error("Day 23 scope inspection skipped: MT5 account is not configured")
        return

    try:
        token = cipher.decrypt(bytes(row["metaapi_token_ciphertext"]))
    except BrokerCredentialDecryptionError:
        logger.error("Day 23 scope inspection failed: stored token could not be decrypted")
        return

    claims = _decode_jwt_payload(token)
    applications: list[str] = []
    roles: list[str] = []
    rule_ids: list[str] = []
    services: list[str] = []
    resource_rules_present = False
    top_level_keys: list[str] = []
    if claims is not None:
        top_level_keys = sorted(str(key) for key in claims.keys())
        (
            applications,
            roles,
            resource_rules_present,
            rule_ids,
            services,
        ) = _safe_scope_names(claims)

    with session_factory() as session:
        session.add(
            AuditEvent(
                actor_user_id=None,
                event_type="mt5.day23_token_scope_inspection",
                entity_type="mt5_account",
                entity_id=row["id"],
                payload={
                    "jwt_payload_decoded": claims is not None,
                    "top_level_keys": top_level_keys,
                    "applications": applications,
                    "access_rule_ids": rule_ids,
                    "services": services,
                    "roles": roles,
                    "resource_rules_present": resource_rules_present,
                    "has_trading_account_management_api": (
                        "trading-account-management-api" in applications
                        or "trading-account-management-api" in rule_ids
                    ),
                    "has_metaapi_rest_api": any(
                        value in applications or value in rule_ids
                        for value in {"metaapi-rest-api", "metaapi-api"}
                    ),
                    "has_metaapi_rpc_api": (
                        "metaapi-rpc-api" in applications
                        or "metaapi-rpc-api" in rule_ids
                    ),
                    "has_metaapi_real_time_streaming_api": (
                        "metaapi-real-time-streaming-api" in applications
                        or "metaapi-real-time-streaming-api" in rule_ids
                    ),
                    "metaapi_request_created": False,
                    "trade_action_created": False,
                },
            )
        )
        session.commit()

    logger.info(
        "Day 23 token scope inspected locally jwt=%s applications=%d rules=%d roles=%d metaapi_request_created=false trade_action_created=false",
        claims is not None,
        len(applications),
        len(rule_ids),
        len(roles),
    )


async def run_day23_acceptance_probe(
    *,
    session_factory: sessionmaker[Session],
    cipher: MetaApiTokenCipher,
) -> None:
    if os.getenv("SUPER_SIGNALS_DAY23_ACCEPTANCE_PROBE", "").strip() != "1":
        return

    owner_id_raw = os.getenv("SUPER_SIGNALS_DAY23_OWNER_ID", "").strip()
    try:
        owner_user_id = UUID(owner_id_raw)
    except ValueError:
        logger.error("Day 23 acceptance probe skipped: owner id is invalid")
        return

    if os.getenv("SUPER_SIGNALS_DAY23_SCOPE_ONLY", "").strip() == "1":
        _record_scope_only(
            session_factory=session_factory,
            cipher=cipher,
            owner_user_id=owner_user_id,
        )
        return

    service = Day23Mt5ReadService(
        session_factory=session_factory,
        cipher=cipher,
        gateway=MetaApiReadGateway(),
    )
    try:
        state = await service.read_owner_live_state(owner_user_id)
    except Day23ReadError as exc:
        logger.error(
            "Day 23 acceptance probe failed code=%s retryable=%s",
            exc.code,
            exc.retryable,
        )
        return

    logger.info(
        "Day 23 acceptance probe completed symbol=%s region=%s positions=%d price_available=%s price_stale=%s execution_ready=%s trade_action_created=false",
        state.price.symbol,
        state.region,
        len(state.positions),
        state.price.available,
        state.price.stale,
        state.execution_ready,
    )
