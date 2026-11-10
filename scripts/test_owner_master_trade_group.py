from datetime import datetime, timedelta, timezone
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import Mock, patch
import unittest

with patch.dict(sys.modules, {"MetaTrader5": Mock(), "msvcrt": Mock()}):
    relay = runpy.run_path(str(Path(__file__).with_name("owner_master_telegram_relay.py")))

NOW = datetime(2026, 11, 10, 13, tzinfo=timezone.utc)
CUTOFF = NOW - timedelta(seconds=120)
BASE = int((NOW.timestamp() + 7200) * 1000)


def deal(ticket, leg, *, entry=0, seconds=-90, reason=3, profit=0, commission=0):
    return N(ticket=ticket, order=ticket+100, position_id=700+leg, entry=entry,
             type=0 if entry == 0 else 1, volume=.03, price=4047.45 if entry == 0 else 4057.10,
             time_msc=BASE+seconds*1000, reason=reason, profit=profit, commission=commission,
             symbol="XAUUSD.s", magic=2601001, comment=f"SS_7b8f7faa_{leg}of3")


class GroupedTelegramTests(unittest.TestCase):
    def events(self, history, positions=(), cutoff=CUTOFF, known=()):
        return dict(relay["group_events"](history, positions, 7200, cutoff, NOW,
                                          lambda d, _: ("4038.19", str(4050+d.position_id-700)), known))

    def test_three_leg_open_and_simultaneous_stop_are_two_messages(self):
        entries = [deal(100+i, i, commission=-.1) for i in (1, 2, 3)]
        exits = [deal(200+i, i, entry=1, seconds=-5, reason=4, profit=-2) for i in (1, 2, 3)]
        result = self.events(entries+exits)
        self.assertEqual(list(result), ["trade-entry:701", "trade-exit:201"])
        self.assertEqual([f["leg"] for f in result["trade-entry:701"]["fills"]], [1, 2, 3])
        self.assertEqual(result["trade-exit:201"]["realised_net"], "-6.3")
        self.assertTrue(result["trade-exit:201"]["trade_closed"])
        self.assertEqual(self.events(entries+exits, known=result), {})

    def test_tp2_protection_waits_for_broker_readback(self):
        entries = [deal(100+i, i) for i in (1, 2, 3)]
        tp2 = deal(202, 2, entry=1, seconds=-5, reason=5, profit=4)
        live = N(identifier=703, price_open=4047.45, sl=4038.19)
        result = self.events(entries+[tp2], [live])
        self.assertNotIn("trade-protection-tp2:701", result)
        live.sl = 4047.45
        result = self.events(entries+[tp2], [live], known=result)
        self.assertEqual(list(result), ["trade-protection-tp2:701"])
        self.assertEqual(result["trade-protection-tp2:701"]["stops"][0]["leg"], 3)

    def test_pre_cutover_entry_is_not_republished(self):
        entries = [deal(100+i, i, seconds=-600) for i in (1, 2, 3)]
        tp2 = deal(202, 2, entry=1, seconds=-5, reason=5, profit=4)
        result = self.events(entries+[tp2])
        self.assertEqual(list(result), ["trade-exit:202"])
        self.assertEqual(result["trade-exit:202"]["source"], "provider")




class BrokerClockRegressionTests(unittest.TestCase):
    def test_old_quote_cannot_change_a_confirmed_two_hour_broker_offset(self):
        scope = relay["broker_offset"].__globals__
        stale_tick = N(time_msc=int((NOW.timestamp()+7200-3600)*1000))
        with patch.dict(scope, {"confirmed_broker_offset": lambda day: 7200, "mt5": N(symbol_info_tick=lambda _: stale_tick)}):
            with self.assertRaisesRegex(RuntimeError, "master_broker_clock_stale"):
                relay["broker_offset"](NOW)

    def test_fresh_quote_uses_the_confirmed_broker_offset(self):
        scope = relay["broker_offset"].__globals__
        tick = N(time_msc=int((NOW.timestamp()+7200)*1000))
        with patch.dict(scope, {"confirmed_broker_offset": lambda day: 7200, "mt5": N(symbol_info_tick=lambda _: tick)}):
            self.assertEqual(relay["broker_offset"](NOW), 7200)

    def test_legacy_published_fills_and_pre_cutover_captures_are_not_republished(self):
        entries = [deal(100+i, i) for i in (1,2,3)]
        exits = [deal(200+i, i, entry=1, seconds=-5, reason=4) for i in (1,2,3)]
        known = {f"deal:{d.ticket}" for d in entries+exits}
        self.assertEqual(dict(relay["group_events"](entries+exits, (), 7200, CUTOFF, NOW,
                              lambda *_: ("4038.19","4052"), known)), {})
        old = [deal(300+i, i, seconds=-600) for i in (1,2,3)]
        self.assertEqual(dict(relay["group_events"](old, (), 7200, CUTOFF, NOW,
                              lambda *_: ("4038.19","4052"))), {})

    def test_unpublished_fill_after_cutover_survives_a_publisher_outage(self):
        old = [deal(300+i, i, seconds=-600) for i in (1,2,3)]
        events = dict(relay["group_events"](old, (), 7200, NOW-timedelta(hours=5), NOW,
                              lambda *_: ("4038.19","4052")))
        self.assertEqual(list(events), ["trade-entry:701"])

if __name__ == "__main__":
    unittest.main()
