"""Fail-closed PU Prime identity regression tests. Never sends broker trades."""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from local_mt5_bridge.mt5_adapter import LocalMt5Error, Mt5Adapter


class FakeTerminal(types.ModuleType):
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_SLTP = 6

    def __init__(self) -> None:
        super().__init__("MetaTrader5")
        self.login = 36394935
        self.server = "PUPrime-Live 6"
        self.sends: list[dict] = []
        self.checks = 0
        self.closed = 0
        self.switch_during_check = False
        self.initialize_args: dict[str, object] | None = None

    def initialize(self, **kwargs: object) -> bool:
        self.initialize_args = kwargs
        return True

    def account_info(self) -> SimpleNamespace:
        return SimpleNamespace(login=self.login, server=self.server)

    def terminal_info(self) -> object:
        return object()

    def shutdown(self) -> None:
        self.closed += 1

    def order_check(self, request: dict) -> SimpleNamespace:
        self.checks += 1
        if self.switch_during_check:
            self.server = "OTHER-SERVER"
        return SimpleNamespace(retcode=0)

    def order_send(self, request: dict) -> SimpleNamespace:
        self.sends.append(request)
        return SimpleNamespace(retcode=10009, order=1234, deal=0, comment="Done")

    def symbol_info(self, name: str) -> SimpleNamespace | None:
        if name == "XAUUSD.s":
            return SimpleNamespace(visible=True, volume_min=0.01, volume_step=0.01)
        return None


class Mt5AccountGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeTerminal()
        self.inject = patch.dict(sys.modules, {"MetaTrader5": self.fake})
        self.inject.start()
        self.addCleanup(self.inject.stop)
        self.adapter = Mt5Adapter(
            terminal_path=r"C:\Program Files\PU Prime MT5 Terminal\terminal64.exe",
            login=36394935,
            password="not-a-real-password",
            server="PUPrime-Live 6",
            magic=2601001,
        )

    def test_connect_accepts_exact_login_and_server(self) -> None:
        self.adapter.connect()
        self.assertEqual(self.fake.closed, 0)

    def test_existing_session_reuses_terminal_without_password(self) -> None:
        self.adapter.use_existing_session = True
        self.adapter.password = ""
        self.adapter.connect()
        self.assertEqual(self.fake.initialize_args.get("path"), self.adapter.terminal_path)
        self.assertNotIn("login", self.fake.initialize_args)
        self.assertNotIn("password", self.fake.initialize_args)
        self.assertEqual(self.fake.closed, 0)

    def test_existing_session_rejects_other_broker_account(self) -> None:
        self.adapter.use_existing_session = True
        self.adapter.password = ""
        self.fake.login = 99999
        with self.assertRaisesRegex(LocalMt5Error, "local_bridge_account_mismatch"):
            self.adapter.connect()
        self.assertEqual(self.fake.closed, 1)

    def test_connect_rejects_wrong_server_even_with_correct_login(self) -> None:
        self.fake.server = "PUPrime-Live6"
        with self.assertRaisesRegex(LocalMt5Error, "local_bridge_account_mismatch"):
            self.adapter.connect()
        self.assertEqual(self.fake.closed, 1)

    def test_execute_rejects_account_switch_before_modifying_position(self) -> None:
        self.adapter.connect()
        self.fake.login = 100
        with self.assertRaisesRegex(LocalMt5Error, "local_bridge_account_mismatch"):
            self.adapter.execute(
                "trade",
                {"actionType": "POSITION_MODIFY", "positionId": 123, "stopLoss": 100},
            )
        self.assertEqual(self.fake.sends, [])

    def test_send_rejects_wrong_server_before_order_send(self) -> None:
        self.fake.server = "OTHER-SERVER"
        with self.assertRaisesRegex(LocalMt5Error, "local_bridge_account_mismatch"):
            self.adapter._send(
                {"action": self.fake.TRADE_ACTION_SLTP, "position": 123, "sl": 100, "tp": 0}
            )
        self.assertEqual(self.fake.sends, [])

    def test_send_rechecks_identity_after_order_check(self) -> None:
        self.fake.switch_during_check = True
        with self.assertRaisesRegex(LocalMt5Error, "local_bridge_account_mismatch"):
            self.adapter._send({"action": self.fake.TRADE_ACTION_DEAL})
        self.assertEqual(self.fake.checks, 1)
        self.assertEqual(self.fake.sends, [])

    def test_correct_account_can_submit_to_fake_terminal(self) -> None:
        result = self.adapter._send(
            {"action": self.fake.TRADE_ACTION_SLTP, "position": 123, "sl": 100, "tp": 0}
        )
        self.assertEqual(result["orderId"], "1234")
        self.assertEqual(len(self.fake.sends), 1)

    def test_pu_prime_gold_symbol_is_selectable(self) -> None:
        self.assertEqual(self.adapter._select_symbol("XAUUSD"), "XAUUSD.s")

    def test_read_only_cloud_probe_confirms_login_server_and_gold(self) -> None:
        self.adapter.connect()
        result = self.adapter.execute("read_terminal_identity", {})
        self.assertEqual(result["login"], 36394935)
        self.assertEqual(result["server"], "PUPrime-Live 6")
        self.assertEqual(result["goldSymbol"], "XAUUSD.s")
        self.assertEqual(result["volumeMin"], 0.01)
        self.assertEqual(self.fake.sends, [])



if __name__ == "__main__":
    unittest.main()
