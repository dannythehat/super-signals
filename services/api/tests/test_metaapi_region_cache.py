import asyncio

from app.metaapi_gateway import MetaApiProvisioningGateway
from app.metaapi_read_gateway import MetaApiReadGateway
from app.metaapi_region_cache import get_metaapi_region


def test_provisioning_reconciliation_primes_execution_region_without_network() -> None:
    account_id = "region-cache-test-account-london"
    state = MetaApiProvisioningGateway._account_state(
        {
            "id": account_id,
            "login": "123456",
            "server": "VantageMarkets-Demo",
            "state": "DEPLOYED",
            "connectionStatus": "CONNECTED",
            "region": "London",
        }
    )

    assert state.account_id == account_id
    assert get_metaapi_region(account_id) == "london"

    gateway = MetaApiReadGateway()

    async def unexpected_request(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("region lookup must not hit MetaAPI after reconciliation")

    gateway._request = unexpected_request  # type: ignore[method-assign]

    assert (
        asyncio.run(
            gateway.resolve_account_region(
                token="unused-test-token",
                account_id=account_id,
            )
        )
        == "london"
    )


def test_invalid_provisioning_region_is_not_cached() -> None:
    account_id = "region-cache-test-account-invalid"
    MetaApiProvisioningGateway._account_state(
        {
            "id": account_id,
            "login": "123456",
            "server": "VantageMarkets-Demo",
            "state": "DEPLOYED",
            "connectionStatus": "CONNECTED",
            "region": "not a valid region!",
        }
    )

    assert get_metaapi_region(account_id) is None
