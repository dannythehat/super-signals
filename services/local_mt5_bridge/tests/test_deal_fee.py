import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from local_mt5_bridge.mt5_adapter import Mt5Adapter


class Fee(unittest.TestCase):
    def test_only_broker_supplied_fee_is_exported(self):
        adapter = object.__new__(Mt5Adapter)
        adapter.mt5 = SimpleNamespace(
            DEAL_ENTRY_IN=0,
            DEAL_ENTRY_OUT=1,
            DEAL_ENTRY_INOUT=2,
            DEAL_ENTRY_OUT_BY=3,
            DEAL_TYPE_BUY=0,
            DEAL_TYPE_SELL=1,
            DEAL_TYPE_BALANCE=2,
            DEAL_TYPE_CREDIT=3,
        )
        item = SimpleNamespace(
            ticket=1,
            order=1,
            position_id=1,
            symbol="XAUUSD",
            type=0,
            entry=0,
            volume=0.01,
            price=4100,
            profit=0,
            commission=-1,
            swap=0,
            time=1,
            comment="fixture",
            reason=3,
            fee=-0.5,
        )
        self.assertEqual(adapter._deal(item)["fee"], -0.5)
        del item.fee
        self.assertIsNone(adapter._deal(item)["fee"])
