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
        (os.getenv("RESEND", "").strip() or os.getenv("RESEND_API_KEY", "").strip()),
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


def send_member_live_welcome(
    *,
    member_email: str,
    display_name: str,
    login_masked: str,
    server: str,
) -> EmailDeliveryResult:
    """Send the polished welcome only after a live Vantage MT5 connection is verified."""

    _api_key, _sender, _admin, public_url = _settings()
    safe_name = html.escape(display_name)
    safe_login = html.escape(login_masked)
    safe_server = html.escape(server)
    account_url = f"{public_url}/account"
    safe_account_url = html.escape(account_url, quote=True)
    safe_logo_url = html.escape(
        f"{public_url}/assets/logo/smart-signals-approved.webp",
        quote=True,
    )

    html_body = f"""
    <div style="margin:0;padding:0;background:#061019;font-family:Arial,Helvetica,sans-serif;color:#eef6fb">
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#061019;margin:0;padding:0">
        <tr><td align="center" style="padding:34px 16px">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="max-width:640px;background:#0a1620;border:1px solid #213746;border-radius:20px;overflow:hidden;box-shadow:0 24px 60px rgba(0,0,0,.35)">
            <tr><td style="padding:28px 30px 22px;border-bottom:1px solid #1b2e3b">
              <img src="{safe_logo_url}" alt="Smart Signals" width="190" style="display:block;width:190px;max-width:68%;height:auto;border:0" />
            </td></tr>
            <tr><td style="padding:34px 30px 12px">
              <div style="display:inline-block;padding:7px 11px;border-radius:999px;background:#0e2d24;border:1px solid #1f614d;color:#91e0bd;font-size:11px;font-weight:800;letter-spacing:.9px">LIVE MT5 CONNECTED ✓</div>
              <h1 style="margin:18px 0 10px;color:#ffffff;font-size:30px;line-height:1.15;font-weight:800">Welcome to Smart Signals</h1>
              <p style="margin:0;color:#b9cad6;font-size:16px;line-height:1.65">Hi {safe_name}, your Smart Signals account is now connected to your live Vantage MT5 account and ready.</p>
            </td></tr>
            <tr><td style="padding:18px 30px">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#07121b;border:1px solid #1c3341;border-radius:16px">
                <tr>
                  <td style="padding:18px;border-bottom:1px solid #172a36;width:50%"><div style="color:#6f899b;font-size:11px;text-transform:uppercase;letter-spacing:.8px">Account status</div><div style="margin-top:6px;color:#9be0c2;font-size:15px;font-weight:800">Active</div></td>
                  <td style="padding:18px;border-bottom:1px solid #172a36"><div style="color:#6f899b;font-size:11px;text-transform:uppercase;letter-spacing:.8px">Trading account</div><div style="margin-top:6px;color:#e5eef4;font-size:15px;font-weight:800">Vantage MT5 · Live</div></td>
                </tr>
                <tr>
                  <td style="padding:18px"><div style="color:#6f899b;font-size:11px;text-transform:uppercase;letter-spacing:.8px">MT5 login</div><div style="margin-top:6px;color:#e5eef4;font-size:15px;font-weight:800">{safe_login}</div></td>
                  <td style="padding:18px"><div style="color:#6f899b;font-size:11px;text-transform:uppercase;letter-spacing:.8px">Server</div><div style="margin-top:6px;color:#e5eef4;font-size:15px;font-weight:800">{safe_server}</div></td>
                </tr>
              </table>
            </td></tr>
            <tr><td style="padding:8px 30px 12px">
              <p style="margin:0;color:#9fb2c0;font-size:14px;line-height:1.65">Smart Signals will now manage approved gold trades through the connected account while your activity and performance are tracked inside the platform.</p>
            </td></tr>
            <tr><td style="padding:18px 30px 34px">
              <a href="{safe_account_url}" style="display:inline-block;background:#28d3b0;color:#03130f;text-decoration:none;font-weight:900;font-size:14px;padding:14px 20px;border-radius:12px">Open Smart Signals</a>
            </td></tr>
            <tr><td style="padding:20px 30px 26px;border-top:1px solid #1b2e3b;background:#07121a">
              <p style="margin:0 0 8px;color:#9fb2c0;font-size:13px;font-weight:700">Smart Signals</p>
              <p style="margin:0;color:#607989;font-size:11px;line-height:1.55">Automated gold trading with full trade tracking. Trading involves risk and losses can occur.</p>
            </td></tr>
          </table>
        </td></tr>
      </table>
    </div>
    """
    text_body = (
        f"Hi {display_name},\n\n"
        "Welcome to Smart Signals. Your live Vantage MT5 account is connected and ready.\n\n"
        f"MT5 login: {login_masked}\nServer: {server}\nStatus: Active\n\n"
        "Smart Signals will manage approved gold trades through your connected account and track your activity and performance.\n\n"
        f"Open Smart Signals: {account_url}\n\n"
        "Trading involves risk and losses can occur."
    )
    return _send(
        to=member_email,
        subject="Welcome to Smart Signals — you're connected ✓",
        html_body=html_body,
        text_body=text_body,
    )


__all__ = [
    "EmailDeliveryResult",
    "send_admin_new_signup",
    "send_member_complimentary_activated",
    "send_member_live_welcome",
]
