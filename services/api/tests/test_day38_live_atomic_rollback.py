from __future__ import annotations

from uuid import UUID

from app.mt5_execution_day38 import Day38LiveUserExecutionService

USER = UUID("11111111-1111-4111-8111-111111111111")


class _Result:
    def __init__(self, row): self.row = row
    def mappings(self): return self
    def first(self): return self.row


class _Session:
    def __init__(self, row): self.row = row
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def execute(self, *args, **kwargs): return _Result(self.row)


class _Factory:
    def __init__(self, row): self.row = row
    def __call__(self): return _Session(self.row)


class _Cipher:
    def decrypt(self, value: bytes) -> str:
        assert value == b"encrypted-live-token"
        return "member-live-terminal-token-123456789"


def test_atomic_rollback_resolves_same_approved_member_live_account() -> None:
    service = object.__new__(Day38LiveUserExecutionService)
    service._session_factory = _Factory({
        "metaapi_account_id": "member-live-metaapi",
        "metaapi_token_ciphertext": b"encrypted-live-token",
        "account_environment": "live",
        "account_status": "connected",
        "login": "777001",
        "server": "VantageInternational-Live",
        "approved_login": "777001",
        "approved_server": "VantageInternational-Live",
    })
    service._cipher = _Cipher()

    account_id, token = service._rollback_account(USER)

    assert account_id == "member-live-metaapi"
    assert token == "member-live-terminal-token-123456789"
