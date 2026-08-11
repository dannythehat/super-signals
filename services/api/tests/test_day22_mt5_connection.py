from cryptography.fernet import Fernet

from app.metaapi_gateway import MetaApiAccountState, MetaApiProvisioningGateway
from app.mt5_connection_manager import (
    DEFAULT_REFRESH_SECONDS,
    MINIMUM_REFRESH_SECONDS,
    Mt5ConnectionManager,
)
from app.mt5_connection_service import Mt5DemoConnectionService
from app.mt5_connection_service_day22 import Day22Mt5DemoConnectionService
from app.mt5_crypto import MetaApiTokenCipher


def test_metaapi_token_cipher_round_trip_and_fingerprint() -> None:
    key = Fernet.generate_key().decode("ascii")
    cipher = MetaApiTokenCipher((key,))
    token = "test-metaapi-token-that-is-long-enough"

    encrypted = cipher.encrypt(token)

    assert encrypted != token.encode("utf-8")
    assert cipher.decrypt(encrypted) == token
    assert len(cipher.fingerprint(token)) == 64


def test_day22_gateway_exposes_no_trade_or_order_method() -> None:
    public_names = {
        name
        for name in dir(MetaApiProvisioningGateway)
        if not name.startswith("_")
    }

    assert public_names == {
        "create_account",
        "deploy_account",
        "find_account",
        "new_transaction_id",
        "read_account",
    }


def test_connected_state_requires_deployed_and_connected() -> None:
    connected = MetaApiAccountState(
        account_id="abc",
        login="12345678",
        server="VantageInternational-Demo",
        state="DEPLOYED",
        connection_status="CONNECTED",
    )
    waiting = MetaApiAccountState(
        account_id="abc",
        login="12345678",
        server="VantageInternational-Demo",
        state="DEPLOYED",
        connection_status="DISCONNECTED",
    )
    failed = MetaApiAccountState(
        account_id="abc",
        login="12345678",
        server="VantageInternational-Demo",
        state="DEPLOY_FAILED",
        connection_status="DISCONNECTED",
    )

    assert Mt5DemoConnectionService._local_status(connected) == "connected"
    assert Mt5DemoConnectionService._local_status(waiting) == "disconnected"
    assert Mt5DemoConnectionService._local_status(failed) == "error"


def test_login_is_masked() -> None:
    assert Mt5DemoConnectionService._mask_login("12345678") == "****5678"
    assert Mt5DemoConnectionService._mask_login("1234") == "****"


def test_password_and_token_are_not_part_of_public_connection_view() -> None:
    fields = set(Mt5DemoConnectionService._empty_view().__dataclass_fields__)
    assert "password" not in fields
    assert "metaapi_token" not in fields


def test_platform_token_can_be_resolved_from_environment_without_database(
    monkeypatch,
) -> None:
    token = "test-metaapi-platform-token-that-is-long-enough"
    monkeypatch.setenv("SUPER_SIGNALS_API", token)
    cipher = MetaApiTokenCipher((Fernet.generate_key().decode("ascii"),))
    service = Day22Mt5DemoConnectionService(
        session_factory=None,  # type: ignore[arg-type]
        cipher=cipher,
        gateway=MetaApiProvisioningGateway(),
    )

    assert service.resolve_platform_token() == token


def test_mt5_manager_defaults_to_hourly_polling(monkeypatch) -> None:
    monkeypatch.delenv("SUPER_SIGNALS_MT5_RECONCILE_SECONDS", raising=False)
    manager = Mt5ConnectionManager(object())  # type: ignore[arg-type]

    assert manager._refresh_seconds == DEFAULT_REFRESH_SECONDS == 3600


def test_mt5_manager_enforces_cost_safety_floor(monkeypatch) -> None:
    monkeypatch.setenv("SUPER_SIGNALS_MT5_RECONCILE_SECONDS", "15")
    manager = Mt5ConnectionManager(object())  # type: ignore[arg-type]

    assert manager._refresh_seconds == MINIMUM_REFRESH_SECONDS == 300
