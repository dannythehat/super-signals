"""Pure house-policy tests; never opens a broker connection or places an order."""
import unittest
from decimal import Decimal
from owner_demo_entry_buffer import (
    GOLD_PIP_SIZE, ENTRY_BUFFER_PIPS, ENTRY_BUFFER_PRICE,
    within_provider_entry_buffer,
)

class EntryToleranceTests(unittest.TestCase):
    def test_pip_definition(self):
        self.assertEqual(GOLD_PIP_SIZE, Decimal("0.1"))
        self.assertEqual(ENTRY_BUFFER_PIPS, Decimal("10"))
        self.assertEqual(ENTRY_BUFFER_PRICE, Decimal("1.0"))

    def test_range_either_side_and_inclusive_boundaries(self):
        for value in ("4109.00", "4110.00", "4111.00", "4112.00", "4113.00"):
            self.assertTrue(within_provider_entry_buffer(value,"4110.00","4112.00"),value)
        self.assertFalse(within_provider_entry_buffer("4108.99","4110.00","4112.00"))
        self.assertFalse(within_provider_entry_buffer("4113.01","4110.00","4112.00"))

    def test_single_price_signal_and_market_sell(self):
        self.assertTrue(within_provider_entry_buffer("4111.00",4112,4112))
        self.assertTrue(within_provider_entry_buffer("4113.00",4112,4112))
        self.assertFalse(within_provider_entry_buffer("4110.99",4112,4112))
        self.assertFalse(within_provider_entry_buffer("4113.01",4112,4112))

    def test_invalid_never_accepted(self):
        for val in ("nan","inf","-inf",None,-1,"wrong"):
            self.assertFalse(within_provider_entry_buffer(val,4110,4112))
        self.assertFalse(within_provider_entry_buffer(4111,4112,4110))
        self.assertFalse(within_provider_entry_buffer(4111,0,4110))
        self.assertFalse(within_provider_entry_buffer(4111,4110,"nan"))

if __name__ == "__main__":
    unittest.main()
