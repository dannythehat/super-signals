"""Smart Signals member subscription claims and owner approval.

The website uses these routes to keep MT5/trading locked until a payment claim has
been reviewed. Payment transaction signatures are customer-supplied references;
owner approval remains the final entitlement decision until automated Solana
verification is wired separately.
"""

from __future__ import annotations

import calendar
from datetime import datetime, timezone
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.access_control import get_current_identity
from app.db import get_db_session
from app.subscription_access import get_subscription_state

DbSession = Annotated[Session, Depends(get_db_session)]
Identity = Annotated[dict[str, Any], Depends(get_current_identity)]

user_router = APIRouter(prefix="/subscription", tags=["subscription-member"])
owner_router = APIRouter(prefix="/subscriptions", tags=["subscription-owner"])

PLAN_PRICES = {"monthly": 99, "annual": 999}
BASE58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


class SubscriptionStateResponse(BaseModel):
    status: str
    active: bool
    plan_code: str | None
    active_until: datetime | None
    pending_claim_id: UUID | None
    pending_plan_code: str | None
    pending_amount_eur: int | None
    pending_transaction_signature: str | None
    last_claim_status: str | None


class MemberAccessStateResponse(BaseModel):
    user_id: UUID
    email: str
    display_name: str | None
    status: str
    active: bool
    plan_code: str | None
    active_until: datetime | None


class MemberAccessMutationResponse(MemberAccessStateResponse):
    message: str


class PaymentClaimRequest(BaseModel):
    plan_code: str = Field(pattern="^(monthly|annual)$")
    transaction_signature: str = Field(min_length=32, max_length=160)


class PaymentClaimResponse(BaseModel):
    claim_id: UUID
    status: str
    plan_code: str
    expected_amount_eur: int
    transaction_signature: str
    message: str


class PendingClaimResponse(BaseModel):
    claim_id: UUID
    user_id: UUID
    email: str
    display_name: str
    plan_code: str
    expected_amount_eur: int
    transaction_signature: str
    submitted_at: datetime


class ReviewRequest(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class ReviewResponse(BaseModel):
    claim_id: UUID
    status: str
    user_id: UUID
    plan_code: str
    active_until: datetime | None = None


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _member(identity: dict[str, Any]) -> None:
    if identity.get("role") != "user":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Member account required.")


def _owner(identity: dict[str, Any]) -> None:
    if identity.get("role") not in {"owner", "admin"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner access required.")


def _state_response(session: Session, user_id: UUID) -> SubscriptionStateResponse:
    state = get_subscription_state(session, user_id)
    return SubscriptionStateResponse(
        status=state.status,
        active=state.active,
        plan_code=state.plan_code,
        active_until=state.active_until,
        pending_claim_id=state.pending_claim_id,
        pending_plan_code=state.pending_plan_code,
        pending_amount_eur=state.pending_amount_eur,
        pending_transaction_signature=state.pending_transaction_signature,
        last_claim_status=state.last_claim_status,
    )


def _managed_member(session: Session, user_id: UUID) -> Any:
    row = session.execute(
        text(
            """
            SELECT DISTINCT u.id AS user_id, u.email, u.display_name
            FROM users u
            JOIN user_roles ur ON ur.user_id=u.id
            JOIN roles r ON r.id=ur.role_id
            WHERE u.id=:user_id AND r.name='user'
            LIMIT 1
            """
        ),
        {"user_id": user_id},
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member account not found.")
    return row


def _member_access_response(
    session: Session,
    member: Any,
    *,
    message: str | None = None,
) -> MemberAccessStateResponse | MemberAccessMutationResponse:
    state = get_subscription_state(session, UUID(str(member["user_id"])))
    values = {
        "user_id": UUID(str(member["user_id"])),
        "email": str(member["email"]),
        "display_name": (str(member["display_name"]) if member["display_name"] else None),
        "status": state.status,
        "active": state.active,
        "plan_code": state.plan_code,
        "active_until": state.active_until,
    }
    if message is None:
        return MemberAccessStateResponse(**values)
    return MemberAccessMutationResponse(**values, message=message)


def _valid_solana_signature(value: str) -> str:
    signature = value.strip()
    if not 32 <= len(signature) <= 160 or any(ch not in BASE58 for ch in signature):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Enter the Solana transaction signature from your USDC payment.",
        )
    return signature


def _add_months(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


@user_router.get("", response_model=SubscriptionStateResponse)
def subscription_status(
    response: Response,
    identity: Identity,
    session: DbSession,
) -> SubscriptionStateResponse:
    _member(identity)
    _no_store(response)
    return _state_response(session, identity["id"])


@user_router.post("/claim", response_model=PaymentClaimResponse, status_code=status.HTTP_201_CREATED)
def submit_payment_claim(
    payload: PaymentClaimRequest,
    response: Response,
    identity: Identity,
    session: DbSession,
) -> PaymentClaimResponse:
    _member(identity)
    signature = _valid_solana_signature(payload.transaction_signature)
    current = get_subscription_state(session, identity["id"])
    if current.pending_claim_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A payment is already awaiting approval for this account.",
        )

    amount = PLAN_PRICES[payload.plan_code]
    try:
        row = session.execute(
            text(
                """
                INSERT INTO subscription_payment_claims
                    (user_id, plan_code, expected_amount_eur, asset, network,
                     transaction_signature, status)
                VALUES
                    (:user_id, :plan_code, :amount, 'USDC', 'solana', :signature, 'pending')
                RETURNING id
                """
            ),
            {
                "user_id": identity["id"],
                "plan_code": payload.plan_code,
                "amount": amount,
                "signature": signature,
            },
        ).mappings().one()
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That transaction has already been submitted.",
        ) from exc

    _no_store(response)
    return PaymentClaimResponse(
        claim_id=UUID(str(row["id"])),
        status="pending",
        plan_code=payload.plan_code,
        expected_amount_eur=amount,
        transaction_signature=signature,
        message="Payment submitted. MT5 unlocks after Smart Signals approves the payment.",
    )


@owner_router.get("/members", response_model=list[MemberAccessStateResponse])
def member_access_states(
    response: Response,
    identity: Identity,
    session: DbSession,
) -> list[MemberAccessStateResponse]:
    _owner(identity)
    members = session.execute(
        text(
            """
            SELECT DISTINCT u.id AS user_id, u.email, u.display_name
            FROM users u
            JOIN user_roles ur ON ur.user_id=u.id
            JOIN roles r ON r.id=ur.role_id
            WHERE r.name='user'
            ORDER BY lower(COALESCE(u.display_name, '')), lower(u.email::text)
            """
        )
    ).mappings().all()
    _no_store(response)
    return [
        _member_access_response(session, member)
        for member in members
    ]


@owner_router.post(
    "/users/{user_id}/pause",
    response_model=MemberAccessMutationResponse,
)
def pause_member_access(
    user_id: UUID,
    response: Response,
    identity: Identity,
    session: DbSession,
) -> MemberAccessMutationResponse:
    _owner(identity)
    member = _managed_member(session, user_id)

    paid_paused = session.execute(
        text(
            """
            UPDATE member_subscriptions
            SET status='suspended', updated_at=now()
            WHERE user_id=:user_id AND status='active'
            RETURNING user_id
            """
        ),
        {"user_id": user_id},
    ).scalar_one_or_none()
    complimentary_paused = session.execute(
        text(
            """
            UPDATE complimentary_access_grants
            SET status='suspended', revoked_at=NULL, updated_at=now()
            WHERE user_id=:user_id AND status='active'
            RETURNING user_id
            """
        ),
        {"user_id": user_id},
    ).scalar_one_or_none()

    if paid_paused is None and complimentary_paused is None:
        current = get_subscription_state(session, user_id)
        if current.status != "suspended":
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": "This member has no active subscription or complimentary access to pause.",
                    "subscription_status": current.status,
                },
            )
    else:
        session.commit()

    _no_store(response)
    return _member_access_response(
        session,
        member,
        message=(
            "Subscription paused. New subscription-gated trading is blocked, while the "
            "member's MT5 connection, settings and history are preserved."
        ),
    )


@owner_router.post(
    "/users/{user_id}/resume",
    response_model=MemberAccessMutationResponse,
)
def resume_member_access(
    user_id: UUID,
    response: Response,
    identity: Identity,
    session: DbSession,
) -> MemberAccessMutationResponse:
    _owner(identity)
    member = _managed_member(session, user_id)

    paid_resumed = session.execute(
        text(
            """
            UPDATE member_subscriptions
            SET status='active', updated_at=now()
            WHERE user_id=:user_id
              AND status='suspended'
              AND active_until > now()
            RETURNING user_id
            """
        ),
        {"user_id": user_id},
    ).scalar_one_or_none()
    complimentary_resumed = session.execute(
        text(
            """
            UPDATE complimentary_access_grants
            SET status='active', revoked_at=NULL, updated_at=now()
            WHERE user_id=:user_id AND status='suspended'
            RETURNING user_id
            """
        ),
        {"user_id": user_id},
    ).scalar_one_or_none()

    if paid_resumed is None and complimentary_resumed is None:
        current = get_subscription_state(session, user_id)
        if current.active:
            session.rollback()
        elif current.status == "suspended":
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": "This paused paid subscription has expired. Approve the member's new payment instead of resuming the old period.",
                    "subscription_status": current.status,
                },
            )
        else:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": "This member does not have paused access to resume.",
                    "subscription_status": current.status,
                },
            )
    else:
        session.commit()

    _no_store(response)
    return _member_access_response(
        session,
        member,
        message="Subscription resumed. Saved MT5 and account settings remain in place.",
    )


@owner_router.get("/pending", response_model=list[PendingClaimResponse])
def pending_payments(
    response: Response,
    identity: Identity,
    session: DbSession,
) -> list[PendingClaimResponse]:
    _owner(identity)
    rows = session.execute(
        text(
            """
            SELECT c.id AS claim_id, c.user_id, u.email, u.display_name,
                   c.plan_code, c.expected_amount_eur, c.transaction_signature,
                   c.submitted_at
            FROM subscription_payment_claims c
            JOIN users u ON u.id = c.user_id
            WHERE c.status = 'pending'
            ORDER BY c.submitted_at ASC
            """
        )
    ).mappings().all()
    _no_store(response)
    return [PendingClaimResponse(**dict(row)) for row in rows]


@owner_router.post("/{claim_id}/approve", response_model=ReviewResponse)
def approve_payment(
    claim_id: UUID,
    payload: ReviewRequest,
    response: Response,
    identity: Identity,
    session: DbSession,
) -> ReviewResponse:
    _owner(identity)
    claim = session.execute(
        text(
            """
            SELECT id, user_id, plan_code, status
            FROM subscription_payment_claims
            WHERE id=:claim_id
            FOR UPDATE
            """
        ),
        {"claim_id": claim_id},
    ).mappings().first()
    if claim is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment claim not found.")
    if claim["status"] != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Payment claim has already been reviewed.")

    now = datetime.now(timezone.utc)
    existing = session.execute(
        text("SELECT active_until FROM member_subscriptions WHERE user_id=:user_id FOR UPDATE"),
        {"user_id": claim["user_id"]},
    ).mappings().first()
    base = now
    if existing and existing["active_until"] and existing["active_until"] > now:
        base = existing["active_until"]
    months = 1 if claim["plan_code"] == "monthly" else 12
    active_until = _add_months(base, months)

    session.execute(
        text(
            """
            INSERT INTO member_subscriptions
                (user_id, plan_code, status, started_at, active_until, last_payment_claim_id)
            VALUES
                (:user_id, :plan_code, 'active', :started_at, :active_until, :claim_id)
            ON CONFLICT (user_id) DO UPDATE SET
                plan_code=EXCLUDED.plan_code,
                status='active',
                active_until=EXCLUDED.active_until,
                last_payment_claim_id=EXCLUDED.last_payment_claim_id,
                updated_at=now()
            """
        ),
        {
            "user_id": claim["user_id"],
            "plan_code": claim["plan_code"],
            "started_at": now,
            "active_until": active_until,
            "claim_id": claim_id,
        },
    )
    session.execute(
        text(
            """
            UPDATE subscription_payment_claims
            SET status='approved', reviewed_at=now(), reviewed_by_user_id=:reviewer,
                review_note=:note, updated_at=now()
            WHERE id=:claim_id
            """
        ),
        {"claim_id": claim_id, "reviewer": identity["id"], "note": payload.note},
    )
    session.commit()
    _no_store(response)
    return ReviewResponse(
        claim_id=claim_id,
        status="approved",
        user_id=UUID(str(claim["user_id"])),
        plan_code=str(claim["plan_code"]),
        active_until=active_until,
    )


@owner_router.post("/{claim_id}/reject", response_model=ReviewResponse)
def reject_payment(
    claim_id: UUID,
    payload: ReviewRequest,
    response: Response,
    identity: Identity,
    session: DbSession,
) -> ReviewResponse:
    _owner(identity)
    claim = session.execute(
        text(
            """
            SELECT id, user_id, plan_code, status
            FROM subscription_payment_claims
            WHERE id=:claim_id
            FOR UPDATE
            """
        ),
        {"claim_id": claim_id},
    ).mappings().first()
    if claim is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment claim not found.")
    if claim["status"] != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Payment claim has already been reviewed.")
    session.execute(
        text(
            """
            UPDATE subscription_payment_claims
            SET status='rejected', reviewed_at=now(), reviewed_by_user_id=:reviewer,
                review_note=:note, updated_at=now()
            WHERE id=:claim_id
            """
        ),
        {"claim_id": claim_id, "reviewer": identity["id"], "note": payload.note},
    )
    session.commit()
    _no_store(response)
    return ReviewResponse(
        claim_id=claim_id,
        status="rejected",
        user_id=UUID(str(claim["user_id"])),
        plan_code=str(claim["plan_code"]),
    )
