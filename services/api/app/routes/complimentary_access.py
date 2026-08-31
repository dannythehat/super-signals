"""Owner-only complimentary member access approval from signup email links."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.access_control import get_current_identity
from app.db import get_db_session
from app.member_email import send_member_complimentary_activated
from app.models import AuditEvent

DbSession = Annotated[Session, Depends(get_db_session)]
Identity = Annotated[dict[str, Any], Depends(get_current_identity)]

router = APIRouter(prefix="/complimentary", tags=["complimentary-access"])


class ComplimentaryGrantRequest(BaseModel):
    token: str = Field(min_length=32, max_length=200)


class ComplimentaryGrantResponse(BaseModel):
    status: str
    user_id: UUID
    email: str
    display_name: str
    member_email_sent: bool
    message: str


def _owner(identity: dict[str, Any]) -> None:
    if identity.get("role") not in {"owner", "admin"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Owner access required.",
        )


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _member_email_already_sent(session: Session, user_id: UUID | str) -> bool:
    return bool(
        session.execute(
            text(
                """
                SELECT EXISTS(
                    SELECT 1
                    FROM audit_events
                    WHERE entity_type='user'
                      AND entity_id=:user_id
                      AND event_type='subscription.complimentary_member_email_sent'
                )
                """
            ),
            {"user_id": user_id},
        ).scalar_one()
    )


def _send_member_email_if_needed(
    session: Session,
    *,
    actor_user_id: UUID | str,
    member_user_id: UUID | str,
    member_email: str,
    display_name: str,
) -> bool:
    """Send the post-approval email once, or recover it after an interrupted request."""
    if _member_email_already_sent(session, member_user_id):
        return True

    delivery = send_member_complimentary_activated(
        member_email=member_email,
        display_name=display_name,
    )
    session.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            event_type=(
                "subscription.complimentary_member_email_sent"
                if delivery.sent
                else "subscription.complimentary_member_email_not_sent"
            ),
            entity_type="user",
            entity_id=member_user_id,
            payload={"reason": delivery.reason},
        )
    )
    session.commit()
    return delivery.sent


def _response(
    *,
    response: Response,
    row: Any,
    member_email_sent: bool,
) -> ComplimentaryGrantResponse:
    _no_store(response)
    return ComplimentaryGrantResponse(
        status="active",
        user_id=UUID(str(row["user_id"])),
        email=str(row["email"]),
        display_name=str(row["display_name"] or "Smart Signals member"),
        member_email_sent=member_email_sent,
        message=(
            "Complimentary access activated. The member has been emailed and can now connect MT5."
            if member_email_sent
            else "Complimentary access activated. The member email could not be sent yet."
        ),
    )


@router.post("/grant", response_model=ComplimentaryGrantResponse)
def grant_complimentary_access(
    payload: ComplimentaryGrantRequest,
    response: Response,
    identity: Identity,
    session: DbSession,
) -> ComplimentaryGrantResponse:
    """Consume one signup token and activate complimentary access for that member.

    The operation is idempotent after a successful grant. If the browser loses the
    original response during a rolling deployment, retrying the same token returns the
    existing active grant instead of failing with "already used". The member email is
    also recovered if the grant committed before delivery completed.
    """
    _owner(identity)
    token_hash = hashlib.sha256(payload.token.strip().encode("utf-8")).hexdigest()

    row = session.execute(
        text(
            """
            SELECT t.id, t.user_id, t.expires_at, t.used_at,
                   u.email, u.display_name, u.status AS user_status
            FROM complimentary_access_tokens AS t
            JOIN users AS u ON u.id=t.user_id
            WHERE t.token_hash=:token_hash
            FOR UPDATE OF t
            """
        ),
        {"token_hash": token_hash},
    ).mappings().first()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This complimentary access link is invalid.",
        )

    if row["used_at"] is not None:
        active_grant = bool(
            session.execute(
                text(
                    """
                    SELECT EXISTS(
                        SELECT 1
                        FROM complimentary_access_grants
                        WHERE user_id=:user_id AND status='active'
                    )
                    """
                ),
                {"user_id": row["user_id"]},
            ).scalar_one()
        )
        if not active_grant:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This complimentary access link has already been used.",
            )

        member_email_sent = _send_member_email_if_needed(
            session,
            actor_user_id=identity["id"],
            member_user_id=row["user_id"],
            member_email=str(row["email"]),
            display_name=str(row["display_name"] or "Smart Signals member"),
        )
        return _response(
            response=response,
            row=row,
            member_email_sent=member_email_sent,
        )

    now = datetime.now(timezone.utc)
    if row["expires_at"] <= now:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="This complimentary access link has expired.",
        )
    if row["user_status"] != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This member account is not active.",
        )

    session.execute(
        text(
            """
            INSERT INTO complimentary_access_grants
                (user_id, status, granted_by_user_id, source, granted_at, revoked_at, updated_at)
            VALUES
                (:user_id, 'active', :owner_id, 'owner_signup_email', :now, NULL, :now)
            ON CONFLICT (user_id) DO UPDATE SET
                status='active',
                granted_by_user_id=EXCLUDED.granted_by_user_id,
                source=EXCLUDED.source,
                granted_at=EXCLUDED.granted_at,
                revoked_at=NULL,
                updated_at=EXCLUDED.updated_at
            """
        ),
        {"user_id": row["user_id"], "owner_id": identity["id"], "now": now},
    )
    session.execute(
        text(
            """
            UPDATE complimentary_access_tokens
            SET used_at=:now
            WHERE id=:token_id
            """
        ),
        {"token_id": row["id"], "now": now},
    )
    session.add(
        AuditEvent(
            actor_user_id=identity["id"],
            event_type="subscription.complimentary_access_granted",
            entity_type="user",
            entity_id=row["user_id"],
            payload={
                "source": "owner_signup_email",
                "paid_subscription_created": False,
                "renewal_required": False,
            },
        )
    )
    session.commit()

    member_email_sent = _send_member_email_if_needed(
        session,
        actor_user_id=identity["id"],
        member_user_id=row["user_id"],
        member_email=str(row["email"]),
        display_name=str(row["display_name"] or "Smart Signals member"),
    )
    return _response(
        response=response,
        row=row,
        member_email_sent=member_email_sent,
    )
