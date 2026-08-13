"""Day 35 trades/failures read-model safety contract."""

from __future__ import annotations

import inspect
from uuid import UUID

from app.operations_day35 import Day35OperationsService
from app.trade_identity import public_trade_identity


def test_operations_failure_query_never_reads_raw_telegram_payload() -> None:
    source = inspect.getsource(Day35OperationsService._current_failures)

    assert "raw_payload" not in source
    assert "metaapi_token_ciphertext" not in source
    assert "password" not in source.lower()


def test_operations_trade_identity_uses_same_public_reference_function() -> None:
    signal_id = UUID("cbc7baf8-8819-4ece-978d-91dbd9f8a9cd")
    identity = public_trade_identity(signal_id)

    assert identity.reference == "SS-CBC7BAF888"
    assert "source" not in identity.reference.lower()
    assert "telegram" not in identity.reference.lower()


def test_operations_service_has_no_broker_mutation_gateway() -> None:
    source = inspect.getsource(Day35OperationsService)

    assert "close_position" not in source
    assert "place" not in source.lower()
    assert "modify_position" not in source
    assert "MetaApiTradeGateway" not in source
