"""No live MT5 dependency: unsuccessful reads must never become empty positions."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from local_mt5_bridge.mt5_adapter import LocalMt5Error, Mt5Adapter


class Reads(unittest.TestCase):
    def test_all_history_and_position_failures_are_explicit(self):
        for result in (None, ()):
            fake = SimpleNamespace(positions_get=lambda: result, orders_get=lambda: result,
                history_orders_get=lambda *a, **k: result,
                history_deals_get=lambda *a, **k: result)
            with patch.dict(sys.modules, {'MetaTrader5': fake}):
                adapter = Mt5Adapter(terminal_path='fixture',login=1,password='',server='demo',magic=1)
            for operation, payload in [
                ('read_positions', {}), ('read_orders', {}),
                ('read_history_orders_by_ticket', {'order_id': '1'}),
                ('read_deals_by_position', {'position_id': '1'}),
                ('read_history_orders_by_time_range', {'start_time':'2026-01-01T00:00:00Z','end_time':'2026-01-02T00:00:00Z'}),
                ('read_deals_by_time_range', {'start_time':'2026-01-01T00:00:00Z','end_time':'2026-01-02T00:00:00Z'}),
            ]:
                with self.subTest(operation=operation, result=result):
                    call = getattr(adapter, '_op_' + operation)
                    if result is None:
                        with self.assertRaisesRegex(LocalMt5Error, 'local_bridge_mt5_read_failed'):
                            call(payload)
                    else:
                        self.assertEqual(call(payload), [])
