"""Owner member subscription routes with PostgreSQL-safe member ordering.

This router is registered before the legacy subscriptions owner router so the
Members & account access page cannot hit the invalid DISTINCT/ORDER BY query.
Pause/resume reuse the canonical subscription mutation functions.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Response
from sqlalchemy import text

from . import subscriptions

router = APIRouter(prefix="/subscriptions", tags=["subscription-owner"])


@router.get("/members", response_model=list[subscriptions.MemberAccessStateResponse])
def member_access_states(
    response: Response,
    identity: subscriptions.Identity,
    session: subscriptions.DbSession,
) -> list[subscriptions.MemberAccessStateResponse]:
    subscriptions._owner(identity)
    members = session.execute(
        text(
            """
            SELECT DISTINCT u.id AS user_id, u.email, u.display_name
            FROM users u
            JOIN user_roles ur ON ur.user_id=u.id
            JOIN roles r ON r.id=ur.role_id
            WHERE r.name='user'
            ORDER BY u.display_name NULLS LAST, u.email
            """
        )
    ).mappings().all()
    subscriptions._no_store(response)
    return [
        subscriptions._member_access_response(session, member)
        for member in members
    ]


@router.post(
    "/users/{user_id}/pause",
    response_model=subscriptions.MemberAccessMutationResponse,
)
def pause_member_access(
    user_id: UUID,
    response: Response,
    identity: subscriptions.Identity,
    session: subscriptions.DbSession,
) -> subscriptions.MemberAccessMutationResponse:
    return subscriptions.pause_member_access(user_id, response, identity, session)


@router.post(
    "/users/{user_id}/resume",
    response_model=subscriptions.MemberAccessMutationResponse,
)
def resume_member_access(
    user_id: UUID,
    response: Response,
    identity: subscriptions.Identity,
    session: subscriptions.DbSession,
) -> subscriptions.MemberAccessMutationResponse:
    return subscriptions.resume_member_access(user_id, response, identity, session)
