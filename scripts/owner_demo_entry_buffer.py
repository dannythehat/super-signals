"""House policy for the sole Contabo Super Signals PU Prime XAUUSD demo master.

A quote within 10 GOLD pips *outside* either end of a provider's entry
range is admissible to the existing, unchanged, broker risk/SL/TP checks.

Gold pip = USD 0.10 on XAUUSD (consistent with performance_ledger_day33).
Do not use the MT5 quote precision (0.01) as one gold pip.
"""
from decimal import Decimal, InvalidOperation

GOLD_PIP_SIZE = Decimal("0.1")
ENTRY_BUFFER_PIPS = Decimal("10")
ENTRY_BUFFER_PRICE = GOLD_PIP_SIZE * ENTRY_BUFFER_PIPS


def within_provider_entry_buffer(price, entry_low, entry_high):
    try:
        price = Decimal(str(price))
        low = Decimal(str(entry_low))
        high = Decimal(str(entry_high))
    except (InvalidOperation, TypeError, ValueError):
        return False
    if not all((price.is_finite(), low.is_finite(), high.is_finite())):
        return False
    if low <= 0 or high <= 0 or price <= 0 or low > high:
        return False
    return low - ENTRY_BUFFER_PRICE <= price <= high + ENTRY_BUFFER_PRICE
