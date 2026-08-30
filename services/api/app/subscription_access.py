"""Shared Smart Signals subscription entitlement checks.

Paid subscription approval or an active complimentary owner grant controls whether an
ordinary member may connect MT5, activate trading, or receive new distributed trades.
Existing open positions are deliberately not invalidated by entitlement expiry/revocation
so they can still be managed and closed safely.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass(frozen=True, slots=True)
class SubscriptionState:
    status: str
    active: bool
    plan_code: str | None
    active_until: datetime | None
    pending_claim_id: UUID | None
    pending_plan_code: str | None
    pending_amount_eur: int | None
    pending_transaction_signature: str | None
    last_claim_status: str | None


def get_subscription_state(session: Session, user_id: UUID) -> SubscriptionState:
    subscription = session.execute(
        text(
            """
            SELECT plan_code, status, active_until
            FROM member_subscriptions
            WHERE user_id=:user_id
            LIMIT 1
            """
        ),
        {"user_id": user_id},
    ).mappings().first()

    complimentary = session.execute(
        text(
            """
            SELECT status
            FROM complimentary_access_grants
            WHERE user_id=:user_id
            LIMIT 1
            """
        ),
        {"user_id": user_id},
    ).mappings().first()

    claim = session.execute(
        text(
            """
            SELECT id, plan_code, expected_amount_eur, transaction_signature, status
            FROM subscription_payment_claims
            WHERE user_id=:user_id
            ORDER BY submitted_at DESC, id DESC
            LIMIT 1
            """
        ),
        {"user_id": user_id},
    ).mappings().first()

    now = datetime.now(timezone.utc)
    active_until = subscription["active_until"] if subscription else None
    paid_active = bool(
        subscription
        and subscription["status"] == "active"
        and active_until is not None
        and active_until > now
    )
    complimentary_active = bool(
        complimentary and complimentary["status"] == "active"
    )
    entitlement_active = paid_active or complimentary_active

    if complimentary_active:
        state_status = "active"
        plan_code = "complimentary"
        effective_active_until = None
    elif paid_active:
        state_status = "active"
        plan_code = str(subscription["plan_code"])
        effective_active_until = active_until
    elif claim and claim["status"] == "pending":
        state_status = "pending"
        plan_code = str(subscription["plan_code"]) if subscription else None
        effective_active_until = active_until
    elif subscription and subscription["status"] == "suspended":
        state_status = "suspended"
        plan_code = str(subscription["plan_code"])
        effective_active_until = active_until
    elif subscription and active_until is not None and active_until <= now:
        state_status = "expired"
        plan_code = str(subscription["plan_code"])
        effective_active_until = active_until
    elif complimentary and complimentary["status"] == "revoked":
        state_status = "revoked"
        plan_code = "complimentary"
        effective_active_until = None
    elif claim and claim["status"] == "rejected":
        state_status = "rejected"
        plan_code = None
        effective_active_until = None
    else:
        state_status = "unpaid"
        plan_code = None
        effective_active_until = None

    pending = claim if claim and claim["status"] == "pending" else None
    return SubscriptionState(
        status=state_status,
        active=entitlement_active,
        plan_code=plan_code,
        active_until=effective_active_until,
        pending_claim_id=(UUID(str(pending["id"])) if pending else None),
        pending_plan_code=(str(pending["plan_code"]) if pending else None),
        pending_amount_eur=(int(pending["expected_amount_eur"]) if pending else None),
        pending_transaction_signature=(str(pending["transaction_signature"]) if pending else None),
        last_claim_status=(str(claim["status"]) if claim else None),
    )


def require_active_subscription(session: Session, user_id: UUID) -> SubscriptionState:
    state = get_subscription_state(session, user_id)
    if state.active:
        return state
    raise HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail={
            "code": "subscription_required",
            "message": (
                "Your Smart Signals access must be active before connecting MT5 or "
                "activating automated trading. Submit your USDC payment for approval first."
            ),
            "subscription_status": state.status,
        },
    )


def subscription_is_active_row(row: Any) -> bool:
    """Small helper for unit-level consumers that already selected subscription fields."""
    if row is None:
        return False
    active_until = row["active_until"]
    return bool(row["status"] == "active" and active_until and active_until > datetime.now(timezone.utc))


__all__ = [
    "SubscriptionState",
    "get_subscription_state",
    "require_active_subscription",
    "subscription_is_active_row",
]
