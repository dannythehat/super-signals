# Day 25 — price and funds gate contract

Super Signals follows the provider. Day 25 does not add trade judgement and does not place, modify or close any trade.

## One-time entry-price rule

The gate consumes one fresh Day 23 quote snapshot. It does not poll, wait, chase the market, choose a better price or retry the signal.

- BUY checks the executable ask once.
- SELL checks the executable bid once.
- The executable price must equal the provider's stated entry.
- If it differs, the stated entry is unavailable and the complete signal is skipped once with `entry_price_unavailable`.
- A different price is never substituted or interpreted as better/worse.
- Stop loss and take profit are not used to judge whether the setup is still attractive or valid.
- Stale or unavailable quotes remain blocked by Day 23 before funds are checked.
- Pending orders are only for explicit provider pending-order instructions and are not invented to wait for a missed market entry.

## All-or-nothing funds rule

Day 24 produces one broker-valid volume for every TP position. Day 25 totals the volume required for the complete TP set and asks the broker/MetaAPI for the margin requirement at the stated executable entry.

- If required margin is less than or equal to current free margin, the complete TP set may proceed to Day 26.
- If required margin exceeds free margin, `insufficient_funds` blocks the complete signal.
- A blocked signal always has `positions_allowed = 0`.
- Partial TP placement is not allowed.
- If the terminal says trading is disabled, the complete signal is blocked.
- If the margin requirement cannot be obtained, the signal is blocked rather than guessed or resized.

## API-call boundary

After a fresh Day 23 snapshot exists:

- price checks: exactly one in-memory executable-price check
- margin checks: zero if price/trading blocks first, otherwise one aggregate margin calculation
- trade requests: zero
- retries/chasing: zero

Day 26 owns exact demo execution of the provider instruction.
