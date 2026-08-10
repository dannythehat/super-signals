from app.metaapi_gateway import MetaApiAccountState
from app.mt5_connection_service import Mt5DemoConnectionService


def test_connected_state_requires_deployed_and_connected():
    connected = MetaApiAccountState(
        account_id="abc", login="12345678", server="VantageInternational-Demo", state="DEPLOYED", connection_status="CONNECTED"
    )
    waiting = MetaApiAccountState(
        account_id="abc", login="12345678", server="VantageInternational-Demo", state="DEPLOYED", connection_status="DISCONNECTED"
    )
    assert Mt5DemoConnectionService._local_status(connected) == "connected"
    assert Mt5DemoConnectionService._local_status(waiting) == "disconnected"


def test_login_is_masked():
    assert Mt5DemoConnectionService._mask_login("12345678") == "****5678"
    assert Mt5DemoConnectionService._mask_login("1234") == "****"


def test_password_is_not_part_of_public_connection_view():
    fields = set(Mt5DemoConnectionService._empty_view().__dataclass_fields__)
    assert "password" not in fields
    assert "metaapi_token" not in fields
