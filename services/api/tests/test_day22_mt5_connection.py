from cryptography.fernet import Fernet

from app.metaapi_gateway import MetaApiAccountState, MetaApiProvisioningGateway
from app.mt5_connection_service import Mt5DemoConnectionService
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
