"""One-time Day 22 recovery helpers for stored MetaAPI credentials.

This module never calls MetaAPI and never handles a Vantage password. It exists
only to re-encrypt and locally verify an already configured MetaAPI token when
the preview-environment encryption key has been lost or rotated.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.models import AuditEvent
from app.mt5_crypto import BrokerCredentialDecryptionError, MetaApiTokenCipher


def reencrypt_existing_metaapi_token(
    *,
    session_factory: sessionmaker[Session],
    cipher: MetaApiTokenCipher,
    owner_user_id: UUID,
    metaapi_token: str,
) -> bool:
    """Re-encrypt the existing owner's MetaAPI token without broker/API calls."""

    token = metaapi_token.strip()
    if len(token) < 20:
        raise ValueError("MetaAPI token is unavailable for recovery.")

    ciphertext = cipher.encrypt(token)
    fingerprint = cipher.fingerprint(token)
    now = datetime.now(UTC)

    with session_factory() as session:
        row = session.execute(
            text(
                """
                SELECT id
                FROM mt5_accounts
                WHERE owner_user_id = :owner_user_id
                LIMIT 1
                FOR UPDATE
                """
            ),
            {"owner_user_id": owner_user_id},
        ).mappings().first()
        if row is None:
            return False

        account_id = row["id"]
        session.execute(
            text(
                """
                UPDATE mt5_accounts
                SET metaapi_token_ciphertext = :ciphertext,
                    metaapi_token_fingerprint = :fingerprint,
                    last_error_code = NULL,
                    updated_at = :updated_at
                WHERE id = :account_id
                """
            ),
            {
                "account_id": account_id,
                "ciphertext": ciphertext,
                "fingerprint": fingerprint,
                "updated_at": now,
            },
        )
        session.add(
            AuditEvent(
                actor_user_id=owner_user_id,
                event_type="mt5.metaapi_token_reencrypted",
                entity_type="mt5_account",
                entity_id=account_id,
                payload={
                    "reason": "day22_preview_key_recovery",
                    "token_encrypted": True,
                    "password_stored": False,
                    "metaapi_request_created": False,
                    "trade_action_created": False,
                },
            )
        )
        session.commit()
        return True


def verify_existing_metaapi_token(
    *,
    session_factory: sessionmaker[Session],
    cipher: MetaApiTokenCipher,
    owner_user_id: UUID,
    expected_token: str,
) -> bool:
    """Round-trip the stored ciphertext locally; never contacts MetaAPI."""

    expected = expected_token.strip()
    expected_usable = len(expected) >= 20
    account_id = None
    ciphertext_present = False
    decrypted = False
    token_match = False
    fingerprint_match = False

    if expected_usable:
        with session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT id, metaapi_token_ciphertext, metaapi_token_fingerprint
                    FROM mt5_accounts
                    WHERE owner_user_id = :owner_user_id
                    LIMIT 1
                    """
                ),
                {"owner_user_id": owner_user_id},
            ).mappings().first()

        if row is not None:
            account_id = row["id"]
            ciphertext = row["metaapi_token_ciphertext"]
            ciphertext_present = ciphertext is not None and len(bytes(ciphertext)) > 0
            if ciphertext_present:
                try:
                    recovered = cipher.decrypt(bytes(ciphertext))
                    decrypted = True
                    token_match = recovered == expected
                except BrokerCredentialDecryptionError:
                    decrypted = False
            fingerprint_match = (
                str(row["metaapi_token_fingerprint"]) == cipher.fingerprint(expected)
            )

    verified = bool(
        expected_usable
        and account_id is not None
        and ciphertext_present
        and decrypted
        and token_match
        and fingerprint_match
    )

    with session_factory() as session:
        session.add(
            AuditEvent(
                actor_user_id=owner_user_id,
                event_type="mt5.metaapi_token_local_verification",
                entity_type="mt5_account",
                entity_id=account_id,
                payload={
                    "expected_token_present": expected_usable,
                    "account_found": account_id is not None,
                    "ciphertext_present": ciphertext_present,
                    "decrypted": decrypted,
                    "token_match": token_match,
                    "fingerprint_match": fingerprint_match,
                    "verified": verified,
                    "metaapi_request_created": False,
                    "trade_action_created": False,
                },
            )
        )
        session.commit()

    return verified
