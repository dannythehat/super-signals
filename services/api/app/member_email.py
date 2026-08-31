"""Private Smart Signals signup and access email delivery.

All recipient addresses are read server-side. No admin destination is emitted to the
website or API. Delivery uses Resend's HTTPS API when configured; signup/access state
never fails merely because email delivery is temporarily unavailable.
"""

from __future__ import annotations

import html
import logging
import os
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EmailDeliveryResult:
    sent: bool
    reason: str | None = None


def _settings() -> tuple[str, str, str, str]:
    return (
        os.getenv("RESEND_API_KEY", "").strip(),
        os.getenv("SMART_SIGNALS_EMAIL_FROM", "").strip(),
        os.getenv("SMART_SIGNALS_ADMIN_EMAIL", "").strip(),
        os.getenv("SMART_SIGNALS_PUBLIC_URL", "https://smartsignals.site").strip().rstrip("/"),
    )


def _safe_resend_error(response: httpx.Response) -> tuple[str, str]:
    """Return Resend's documented error type/message without logging request secrets."""

    error_type = "unknown"
    message = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            raw_type = body.get("name") or body.get("type") or body.get("error")
            raw_message = body.get("message")
            if isinstance(raw_type, str) and raw_type.strip():
                error_type = raw_type.strip()[:80]
            if isinstance(raw_message, str) and raw_message.strip():
                message = raw_message.strip()[:500]
    except Exception:
        pass
    return error_type, message


def _send(*, to: str, subject: str, html_body: str, text_body: str) -> EmailDeliveryResult:
    api_key, sender, _admin, _public_url = _settings()
    if not api_key or not sender or not to.strip():
        logger.warning("Member email skipped because outbound email transport is not configured")
        return EmailDeliveryResult(False, "email_transport_not_configured")

    try:
        response = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "from": sender,
                "to": [to.strip()],
                "subject": subject,
                "html": html_body,
                "text": text_body,
            },
            timeout=8.0,
        )
        if response.is_error:
            error_type, message = _safe_resend_error(response)
            logger.error(
                "Resend email rejected status=%s type=%s message=%s",
                response.status_code,
                error_type,
                message or "(no message)",
            )
            return EmailDeliveryResult(False, f"resend_{error_type.lower()}")
    except Exception:
        logger.exception("Member email delivery failed before Resend returned a response")
        return EmailDeliveryResult(False, "email_delivery_failed")
    return EmailDeliveryResult(True)


def send_admin_new_signup(*, member_email: str, display_name: str, complimentary_token: str) -> EmailDeliveryResult:
    _api_key, _sender, admin_email, public_url = _settings()
    if not admin_email:
        logger.warning("New-signup email skipped because admin destination is not configured")
        return EmailDeliveryResult(False, "admin_email_not_configured")

    safe_name = html.escape(display_name)
    safe_email = html.escape(member_email)
    approval_url = f"{public_url}/complimentary?token={complimentary_token}"
    safe_url = html.escape(approval_url, quote=True)

    html_body = f"""
    <div style="font-family:Arial,sans-serif;background:#071018;color:#eef8ff;padding:28px">
      <div style="max-width:620px;margin:auto;background:#0b1722;border:1px solid #244252;border-radius:18px;padding:28px">
        <div style="font-size:12px;letter-spacing:1.5px;color:#67e8f9;font-weight:700">SMART SIGNALS</div>
        <h1 style="font-size:24px;margin:10px 0 18px;color:#ffffff">New Smart Signals signup</h1>
        <p style="color:#c8d8e5;line-height:1.6">A new member created a Smart Signals account and is waiting for owner approval.</p>
        <div style="background:#08131c;border:1px solid #1b3443;border-radius:14px;padding:16px;margin:18px 0">
          <div style="margin-bottom:8px"><strong>Name:</strong> {safe_name}</div>
          <div><strong>Email:</strong> {safe_email}</div>
        </div>
        <p style="color:#c8d8e5;line-height:1.6">Use the private owner action below to approve this member. Once approved, Smart Signals will automatically email them with instructions to sign in and connect their Vantage MT5 account.</p>
        <p style="margin:24px 0">
          <a href="{safe_url}" style="display:inline-block;background:#22c55e;color:#04110a;text-decoration:none;font-weight:800;padding:14px 20px;border-radius:12px">Approve Member Access</a>
        </p>
        <p style="font-size:12px;color:#7f9cad;line-height:1.5">This approval link is private, expires after 7 days and can only be completed from an authenticated Smart Signals owner account.</p>
      </div>
    </div>
    """
    text_body = (
        "New Smart Signals signup\n\n"
        f"Name: {display_name}\nEmail: {member_email}\n\n"
        "Approve member access (owner login required):\n"
        f"{approval_url}\n\n"
        "After approval, Smart Signals automatically emails the member to connect their Vantage MT5 account.\n"
        "The private approval link expires after 7 days."
    )
    return _send(
        to=admin_email,
        subject=f"Approval needed — Smart Signals signup — {display_name}",
        html_body=html_body,
        text_body=text_body,
    )


def send_member_complimentary_activated(*, member_email: str, display_name: str) -> EmailDeliveryResult:
    _api_key, _sender, _admin, public_url = _settings()
    safe_name = html.escape(display_name)
    continue_url = f"{public_url}/join#mt5"
    safe_url = html.escape(continue_url, quote=True)
    html_body = f"""
    <div style="font-family:Arial,sans-serif;background:#071018;color:#eef8ff;padding:28px">
      <div style="max-width:620px;margin:auto;background:#0b1722;border:1px solid #244252;border-radius:18px;padding:28px">
        <div style="font-size:12px;letter-spacing:1.5px;color:#67e8f9;font-weight:700">SMART SIGNALS</div>
        <h1 style="font-size:24px;margin:10px 0 18px;color:#ffffff">Your Smart Signals access is approved</h1>
        <p style="color:#c8d8e5;line-height:1.6">Hi {safe_name}, your Smart Signals account has been approved and is ready for setup.</p>
        <p style="color:#c8d8e5;line-height:1.6">Sign in, open your trading-account setup and enter the three details supplied by Vantage: your MT5 account number, MT5 trading password and exact MT5 server. Smart Signals does not store your MT5 password.</p>
        <p style="margin:24px 0">
          <a href="{safe_url}" style="display:inline-block;background:#22c55e;color:#04110a;text-decoration:none;font-weight:800;padding:14px 20px;border-radius:12px">Connect Vantage MT5</a>
        </p>
      </div>
    </div>
    """
    text_body = (
        f"Hi {display_name},\n\n"
        "Your Smart Signals account has been approved.\n\n"
        "Next step: sign in and connect your Vantage MT5 account using the MT5 account number, trading password and exact server supplied by Vantage. Smart Signals does not store your MT5 password.\n\n"
        f"Connect MT5: {continue_url}"
    )
    return _send(
        to=member_email,
        subject="Approved — connect your Vantage MT5 account",
        html_body=html_body,
        text_body=text_body,
    )


__all__ = [
    "EmailDeliveryResult",
    "send_admin_new_signup",
    "send_member_complimentary_activated",
]
