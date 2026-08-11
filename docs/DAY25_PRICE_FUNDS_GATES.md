# Day 25 — price and funds gate contract

Day 25 decides whether a complete already-sized signal may proceed to Day 26. It does not place, modify or close any trade.

## One-time entry-price rule

The gate consumes one fresh Day 23 quote snapshot. It does not poll, wait, chase the market or retry the signal.

- BUY uses the executable ask.
- SELL uses the executable bid.
- BUY is available when ask is equal to or below the signal entry **and still strictly above the signal stop loss**.
- SELL is available when bid is equal to or above the signal entry **and still strictly below the signal stop loss**.
- A same-or-better price may proceed only while the original signal geometry remains valid.
- If price has moved through the stated entry against the user, the signal is blocked once with `entry_price_unavailable`.
- If a superficially better price has already moved to or through the signal stop, the signal is also blocked with `entry_price_unavailable` rather than resurrecting a stopped-out setup.
- Stale or unavailable quotes remain blocked by Day 23 before margin is checked.

Using only same-or-better prices while preserving the stop boundary means Day 25 never increases stop distance beyond the Day 24 risk calculation that used the signal's stated entry.

## All-or-nothing funds rule

Day 24 produces one volume per TP position. Day 25 multiplies that volume by the number of TP positions and asks MetaAPI/Vantage for one broker-side margin calculation for the aggregate proposed volume at the executable price.

This deliberately avoids guessing XAUUSD margin from leverage or hard-coded contract assumptions.

- If required margin is less than or equal to current free margin, the complete TP set may proceed.
- If required margin exceeds free margin, `insufficient_funds` blocks the complete signal.
- A blocked signal always has `positions_allowed = 0`.
- Partial TP placement is not allowed.
- If the terminal says trading is disabled, the complete signal is blocked.
- If broker margin cannot be calculated, the signal is blocked rather than guessed or retried.

## API-call boundary

After a fresh Day 23 snapshot exists:

- price checks: exactly one in-memory executable-price evaluation
- margin checks: zero if price/trading blocks first, otherwise one aggregate `calculate-margin` request
- trade requests: zero

Day 26 owns actual demo position placement.
