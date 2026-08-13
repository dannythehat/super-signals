"""Day 35 member MT5 onboarding contract tests."""

from __future__ import annotations

import inspect

from app.mt5_onboarding_day35 import Day35Mt5OnboardingService
from app.routes.mt5_onboarding_day35 import (
    ConnectApprovedMt5Request,
    MemberMt5OnboardingResponse,
    PendingMt5ApprovalRequestResponse,
    SubmitMt5ApprovalRequest,
)


def test_member_submits_only_account_number_and_server_for_owner_review() -> None:
    assert set(SubmitMt5ApprovalRequest.model_fields) == {"login", "server"}
    submit_parameters = set(inspect.signature(Day35Mt5OnboardingService.submit_request).parameters)
    assert submit_parameters == {"self", "user_id", "login", "server"}


def test_trading_password_is_requested_only_after_owner_approval() -> None:
    assert set(ConnectApprovedMt5Request.model_fields) == {"password"}
    connect_parameters = set(inspect.signature(Day35Mt5OnboardingService.connect_approved).parameters)
    assert connect_parameters == {"self", "user_id", "password"}


def test_owner_review_surface_never_exposes_password_or_metaapi_credentials() -> None:
    fields = set(PendingMt5ApprovalRequestResponse.model_fields)
    assert fields == {
        "request_id", "user_id", "email", "display_name", "login_masked",
        "server", "requested_at", "updated_at",
    }
    forbidden = {field for field in fields if any(term in field.casefold() for term in ("password", "token", "metaapi", "cipher"))}
    assert forbidden == set()


def test_member_status_contains_onboarding_state_but_no_secret_fields() -> None:
    fields = set(MemberMt5OnboardingResponse.model_fields)
    assert {"request_status", "approved", "connection_status"} <= fields
    forbidden = {field for field in fields if any(term in field.casefold() for term in ("password", "token", "metaapi", "cipher"))}
    assert forbidden == set()
