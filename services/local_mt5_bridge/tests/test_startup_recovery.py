"""Connection recovery never claims a command before broker identity is available."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from local_mt5_bridge.main import BridgeWorker
from local_mt5_bridge.mt5_adapter import LocalMt5Error


class Recovery(unittest.TestCase):
    def worker(self):
        w = object.__new__(BridgeWorker)
        w.settings = SimpleNamespace(
            worker_id="fixture", profile="demo", expected_account_id="fixture", poll_seconds=0.25
        )
        w.mt5 = Mock()
        w._health = Mock()
        w._process = Mock()
        return w

    def test_startup_failure_retries_before_any_claim(self):
        w = self.worker()
        w.mt5.connect.side_effect = [LocalMt5Error("ipc"), None]
        w._claim = Mock(side_effect=KeyboardInterrupt)
        with patch("local_mt5_bridge.main.time.sleep") as sleep:
            w.run()
        self.assertEqual(w.mt5.connect.call_count, 2)
        w.mt5.close.assert_called_once()
        w._claim.assert_called_once()
        w._process.assert_not_called()
        sleep.assert_called_once_with(5.0)
        w._health.assert_any_call("disconnected", error="ipc")

    def test_persistent_connection_failure_never_claims(self):
        w = self.worker()
        w.mt5.connect.side_effect = LocalMt5Error("ipc")
        w._claim = Mock()
        with patch("local_mt5_bridge.main.time.sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                w.run()
        w._claim.assert_not_called()
        w._process.assert_not_called()

    def test_failed_broker_command_retains_degraded_health(self):
        w = self.worker()
        del w._process
        w.journal = Mock()
        w.journal.begin.return_value = ("new", None)
        w._complete = Mock()
        w.mt5.execute.side_effect = LocalMt5Error("local_bridge_mt5_read_failed")
        w._process({"id": "fixture", "account_id": "fixture", "operation": "read_positions"})
        self.assertEqual(w._broker_health, "broker_unavailable")
        w._health.assert_called_once_with(
            "broker_unavailable", error="local_bridge_mt5_read_failed", progress=True
        )
        self.assertEqual(w._complete.call_args.kwargs["status"], "failed")

    def test_unknown_submission_is_ambiguous_and_never_executed_twice(self):
        w=self.worker();del w._process
        w.journal=Mock();w.journal.begin.return_value=('new',None);w._complete=Mock()
        w.mt5.execute.side_effect=LocalMt5Error('local_bridge_trade_result_missing')
        command={'id':'fixture','account_id':'fixture','operation':'trade'}
        w._process(command)
        saved=w._complete.call_args.kwargs
        self.assertEqual(saved['status'],'ambiguous')
        w.journal.begin.return_value=('completed',saved)
        w._process(command)
        w.mt5.execute.assert_called_once()

    def test_runtime_disconnect_recovers_before_claiming_any_command(self):
        w=self.worker()
        w.mt5._assert_expected_account.side_effect=[LocalMt5Error('ipc'),KeyboardInterrupt]
        w.mt5.connect.side_effect=[None,LocalMt5Error('ipc')]
        w._claim=Mock()
        with patch('local_mt5_bridge.main.time.sleep'):
            w.run()
        w._claim.assert_not_called()
        w.mt5.close.assert_called_once()
        w._health.assert_any_call('disconnected',error='ipc')
