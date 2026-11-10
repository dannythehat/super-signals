"""Mandatory broker-only TP2 house-lock tests. No live trade actions."""
import unittest
from types import SimpleNamespace as N
from tp2_house_lock import (
    MASTER_MAGIC, FOLLOWER_MAGIC, needs_stop_upgrade, confirmed_tp2,
    _apply_entry_stop, master_groups,
)
class FakeMt5:
    DEAL_ENTRY_IN=0; DEAL_ENTRY_OUT=1; DEAL_ENTRY_OUT_BY=3; DEAL_REASON_TP=5
    TRADE_ACTION_SLTP=6; TRADE_RETCODE_DONE=10009
    def __init__(self,side=0,sl=90,entry=100,comment="SS_12345678_3of4",magic=MASTER_MAGIC):
        self.position=N(ticket=45,magic=magic,comment=comment,symbol="XAUUSD.s",
                        type=side,sl=sl,tp=130,price_open=entry)
        self.requests=[]
    def symbol_info(self, symbol):
        return N(point=0.01,digits=2,trade_stops_level=10,trade_freeze_level=0)
    def symbol_info_tick(self, symbol):
        return N(bid=110,ask=110)
    def order_send(self,req):
        self.requests.append(dict(req))
        self.position.sl=req["sl"]
        return N(retcode=self.TRADE_RETCODE_DONE)
    def positions_get(self,ticket=None):
        return [self.position]
    def last_error(self):return (0,"")
class TestHouseLock(unittest.TestCase):
    def test_never_loosen_buy_or_sell(self):
        self.assertFalse(needs_stop_upgrade(0,101,100,.01))
        self.assertFalse(needs_stop_upgrade(1,99,100,.01))
        self.assertTrue(needs_stop_upgrade(0,99,100,.01))
        self.assertTrue(needs_stop_upgrade(1,101,100,.01))
        self.assertFalse(needs_stop_upgrade(0,100,100,.01))
    def test_broker_tp2_required_and_no_partial(self):
        opened=N(ticket=1,magic=MASTER_MAGIC,entry=0,type=0,position_id=10,price=99,time=1)
        closed=N(ticket=2,magic=MASTER_MAGIC,entry=1,type=1,position_id=10,reason=5,price=101,time=2)
        self.assertIsNotNone(confirmed_tp2(FakeMt5,1,[opened,closed],[],MASTER_MAGIC))
        self.assertIsNone(confirmed_tp2(FakeMt5,1,[opened,closed],[N(ticket=10)],MASTER_MAGIC))
        self.assertIsNone(confirmed_tp2(FakeMt5,1,[opened,N(**{**vars(closed),"reason":4})],[],MASTER_MAGIC))
        self.assertIsNone(confirmed_tp2(FakeMt5,1,[opened,N(**{**vars(closed),"price":98})],[],MASTER_MAGIC))
        self.assertIsNone(confirmed_tp2(FakeMt5,1,[opened,closed],[],FOLLOWER_MAGIC))
    def test_only_mapped_survivor_and_broker_readback(self):
        fake=FakeMt5()
        self.assertTrue(_apply_entry_stop(fake,fake.position,MASTER_MAGIC,"SS_12345678_3of4"))
        self.assertEqual(len(fake.requests),1)
        self.assertEqual(fake.requests[0]["position"],45)
        self.assertEqual(fake.requests[0]["sl"],100)
        self.assertEqual(fake.requests[0]["tp"],130)
        self.assertNotIn("volume",fake.requests[0])
        self.assertEqual(fake.position.sl,100)
        self.assertFalse(_apply_entry_stop(fake,fake.position,MASTER_MAGIC,"SS_12345678_3of4"))
        self.assertEqual(len(fake.requests),1)
    def test_no_unrelated_position_touch(self):
        fake=FakeMt5()
        with self.assertRaisesRegex(RuntimeError,"comment_mismatch"):
            _apply_entry_stop(fake,fake.position,MASTER_MAGIC,"SS_deadbeef_3of4")
        with self.assertRaisesRegex(RuntimeError,"magic_mismatch"):
            _apply_entry_stop(fake,fake.position,FOLLOWER_MAGIC,"SS_12345678_3of4")
        self.assertEqual(fake.requests,[])
    def test_own_follower_entry_for_sell(self):
        fake=FakeMt5(side=1,sl=120,entry=101,comment="SSC438544647",magic=FOLLOWER_MAGIC)
        fake.symbol_info_tick=lambda symbol:N(bid=93,ask=94)
        self.assertTrue(_apply_entry_stop(fake,fake.position,FOLLOWER_MAGIC,"SSC438544647"))
        self.assertEqual(fake.requests[0]["sl"],101)
        self.assertEqual(fake.requests[0]["tp"],130)
    def test_no_modify_if_price_already_reversed(self):
        fake=FakeMt5()
        fake.symbol_info_tick=lambda symbol:N(bid=99.9,ask=100.1)
        with self.assertRaisesRegex(RuntimeError,"broker_distance_or_price_reversal"):
            _apply_entry_stop(fake,fake.position,MASTER_MAGIC,"SS_12345678_3of4")
        self.assertEqual(fake.requests,[])
if __name__=="__main__":unittest.main()
