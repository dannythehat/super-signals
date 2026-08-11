# Day 24 — per-position risk sizing contract

Day 24 calculates broker-valid position volume. It does **not** invent or change a signal's entry, stop loss or take-profit values.

## User settings

Allowed normal risk settings per TP position:

- 0.5%
- 1% — recommended UI preset
- 1.5%
- 2%

The later user controls present **1% + Allow double-lot signals ON** as the recommended preset. Users may change either setting.

## Double-lot rule

Two independent facts are required:

1. the parsed Telegram signal explicitly requests double lot size
2. the user has `Allow double-lot signals` enabled

Only when both are true does the effective per-position risk double:

- 0.5% → 1%
- 1% → 2%
- 1.5% → 3%
- 2% → 4%

If the signal requests double size but the user has turned the setting off, normal selected risk is used.

## Sizing inputs

The engine receives:

- account balance
- user's selected risk percentage
- parsed signal entry
- parsed signal stop loss
- number of TP positions
- broker tick size
- broker tick value
- broker minimum volume
- broker maximum volume
- broker volume step
- signal double-lot flag
- user double-lot approval flag

## Safety invariant

Broker volume is rounded **down** to a valid step. Rounding may reduce actual risk below the target but must never increase it above the effective per-position risk budget.

If the broker minimum volume itself would exceed the user's risk budget, sizing fails instead of forcing the minimum.

Day 24 has no order-placement method and makes no MetaAPI network request.