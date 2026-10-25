"""Account-scoped PU bridge tests. No MT5 connection, order or deployment."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.local_bridge_queue import LocalBridgeQueue
from app.metaapi_gateway import MetaApiGatewayError
from app.routes.local_bridge import WorkerIdentity, _authorize, _scope


def _identity(account_id: str = "pu-account-ref", profile: str = "pu-prime") -> WorkerIdentity:
    return WorkerIdentity(
        worker_id="contabo-pu-master", profile=profile, account_id=account_id,
        version="0.1.0",
    )


def _separate_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPER_SIGNALS_LOCAL_BRIDGE_TOKEN", "a" * 40)
    monkeypatch.setenv("SUPER_SIGNALS_PU_BRIDGE_TOKEN", "b" * 40)
    monkeypatch.setenv("SUPER_SIGNALS_PU_BRIDGE_PROFILE", "pu-prime")
    monkeypatch.setenv("SUPER_SIGNALS_PU_BRIDGE_ACCOUNT_ID", "pu-account-ref")


def test_pu_token_can_only_identify_matching_account(monkeypatch: pytest.MonkeyPatch) -> None:
    _separate_credentials(monkeypatch)
    scope = _authorize("Bearer " + "b" * 40)
    assert scope == "pu"
    assert _scope(_identity(), scope) == ("pu-prime", "pu-account-ref")


def test_pu_token_rejects_mismatched_account(monkeypatch: pytest.MonkeyPatch) -> None:
    _separate_credentials(monkeypatch)
    with pytest.raises(HTTPException) as error:
        _scope(_identity("different-account"), "pu")
    assert error.value.status_code == 403


def test_pu_token_rejects_wrong_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    _separate_credentials(monkeypatch)
    with pytest.raises(HTTPException) as error:
        _scope(_identity("pu-account-ref", "super-signals"), "pu")
    assert error.value.status_code == 403


def test_pu_token_fails_closed_without_server_account_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _separate_credentials(monkeypatch)
    monkeypatch.delenv("SUPER_SIGNALS_PU_BRIDGE_ACCOUNT_ID")
    with pytest.raises(HTTPException) as error:
        _scope(_identity(), "pu")
    assert error.value.status_code == 503


def test_legacy_token_cannot_claim_pu_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    _separate_credentials(monkeypatch)
    monkeypatch.setenv("SUPER_SIGNALS_LOCAL_BRIDGE_PROFILE", "super-signals")
    scope = _authorize("Bearer " + "a" * 40)
    assert scope == "legacy"
    with pytest.raises(HTTPException) as error:
        _scope(_identity(), scope)
    assert error.value.status_code == 403


def test_pu_token_must_not_equal_legacy_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _separate_credentials(monkeypatch)
    monkeypatch.setenv("SUPER_SIGNALS_PU_BRIDGE_TOKEN", "a" * 40)
    assert _authorize("Bearer " + "a" * 40) == "legacy"
    with pytest.raises(HTTPException) as error:
        _scope(_identity(), "legacy")
    assert error.value.status_code == 403


def test_scoped_enqueue_rejects_other_account_before_database_access() -> None:
    factory = MagicMock()
    queue = LocalBridgeQueue(factory, profile="pu-prime", account_id="pu-account-ref")
    with pytest.raises(MetaApiGatewayError):
        queue._enqueue(
            account_id="different-account",
            operation="trade",
            payload={"actionType": "ORDER_TYPE_BUY"},
            idempotency_key="some-test-key",
        )
    factory.assert_not_called()


def test_legacy_profile_has_no_forced_account_binding() -> None:
    queue = LocalBridgeQueue(MagicMock(), profile="super-signals")
    assert queue.account_id is None


def test_complete_cannot_cross_profile_even_for_terminal_status() -> None:
    command = MagicMock()
    command.profile = "super-signals"
    command.account_id = "different-account"
    command.status = "succeeded"
    session = MagicMock()
    session.get.return_value = command
    session.__enter__.return_value = session
    factory = MagicMock(return_value=session)
    queue = LocalBridgeQueue(factory, profile="pu-prime", account_id="pu-account-ref")
    with pytest.raises(PermissionError, match="local_bridge_scope_mismatch"):
        queue.complete(
            command_id=MagicMock(),
            worker_id="contabo-pu-master",
            lease_token=MagicMock(),
            status="succeeded",
        )


@pytest.mark.asyncio
async def test_authenticated_pu_smoke_does_not_send_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _separate_credentials(monkeypatch)
    from app.routes.local_bridge import pu_connection_smoke

    fake_queue = MagicMock()
    fake_queue.execute = AsyncMock(
        return_value={
            "login": 36394935,
            "server": "PUPrime-Live 6",
            "goldSymbol": "XAUUSD.s",
            "volumeMin": 0.01,
            "volumeStep": 0.01,
        }
    )
    with patch("app.routes.local_bridge.LocalBridgeQueue", return_value=fake_queue):
        with patch("app.routes.local_bridge.get_session_factory", return_value=MagicMock()):
            result = await pu_connection_smoke(authorization_scope="pu")
    assert result["status"] == "connected"
    assert result["terminal"]["login"] == 36394935
    fake_queue.execute.assert_awaited_once_with(
        account_id="pu-account-ref",
        operation="read_terminal_identity",
        payload={},
    )


@pytest.mark.asyncio
async def test_legacy_token_cannot_run_pu_smoke() -> None:
    from app.routes.local_bridge import pu_connection_smoke

    with pytest.raises(HTTPException) as error:
        await pu_connection_smoke(authorization_scope="legacy")
    assert error.value.status_code == 403
