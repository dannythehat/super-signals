from app import member_email


def test_signup_and_approval_emails_point_members_to_mt5(monkeypatch) -> None:
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    monkeypatch.setenv("SMART_SIGNALS_EMAIL_FROM", "Smart Signals <noreply@example.com>")
    monkeypatch.setenv("SMART_SIGNALS_ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("SMART_SIGNALS_PUBLIC_URL", "https://smartsignals.site")

    sent: list[dict[str, str]] = []

    def fake_send(*, to: str, subject: str, html_body: str, text_body: str):
        sent.append({"to": to, "subject": subject, "html": html_body, "text": text_body})
        return member_email.EmailDeliveryResult(True)

    monkeypatch.setattr(member_email, "_send", fake_send)

    owner_result = member_email.send_admin_new_signup(
        member_email="member@example.com",
        display_name="Test Member",
        complimentary_token="x" * 40,
    )
    member_result = member_email.send_member_complimentary_activated(
        member_email="member@example.com",
        display_name="Test Member",
    )

    assert owner_result.sent is True
    assert member_result.sent is True
    assert sent[0]["to"] == "owner@example.com"
    assert "Approval needed" in sent[0]["subject"]
    assert "Approve Member Access" in sent[0]["html"]
    assert "/complimentary?token=" in sent[0]["html"]
    assert sent[1]["to"] == "member@example.com"
    assert "connect your Vantage MT5 account" in sent[1]["subject"]
    assert "MT5 account number" in sent[1]["html"]
    assert "MT5 trading password" in sent[1]["html"]
    assert "exact MT5 server" in sent[1]["html"]
    assert "/join#mt5" in sent[1]["html"]
