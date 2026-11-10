"""Pure mandatory TP2 house safety tests, no live broker orders."""
import unittest
from types import SimpleNamespace as N
from tp2_house_lock import (
    MASTER_MAGIC, FOLLOWER_MAGIC, master_groups,
    needs_stop_upgrade, confirmed_tp2,
)
class FakeMt5:
    DEAL_ENTRY_IN = 0
    DEAL_ENTRY_OUT = 1
    DEAL_ENTRY_OUT_BY = 3
    DEAL_REASON_TP = 5
class TestHouseLock(unittest.TestCase):
    def test_never_loosen(self):
        self.assertFalse(needs_stop_upgrade(0, 101, 100, 0.01))
        self.assertFalse(needs_stop_upgrade(1, 99, 100, 0.01))
        self.assertTrue(needs_stop_upgrade(0, 99, 100, 0.01))
        self.assertTrue(needs_stop_upgrade(1, 101, 100, 0.01))
    def test_require_tp_broker_confirmation(self):
        opened=N(ticket=1, magic=MASTER_MAGIC,entry=0,type=0,position_id=10,price=99,time=1)
        closed=N(ticket=2,magic=MASTER_MAGIC,entry=1,type=1,position_id=10,reason=5,price=101,time=2)
        self.assertIsNotNone(confirmed_tp2(FakeMt5,1,[opened,closed],[],MASTER_MAGIC))
        self.assertIsNone(confirmed_tp2(FakeMt5,1,[opened,closed],[N(ticket=10)],MASTER_MAGIC))
        self.assertIsNone(confirmed_tp2(FakeMt5,1,[opened,N(**{**vars(closed),"reason":4})],[],MASTER_MAGIC))
        self.assertIsNone(confirmed_tp2(FakeMt5,1,[opened,N(**{**vars(closed),"price":98})],[],MASTER_MAGIC))
if __name__=="__main__":unittest.main()
