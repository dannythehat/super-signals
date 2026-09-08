# Provider Intelligence Day 18 — Combined-book engine + account-mode constraint

Date: 2026-09-08

Canonical contract: Day 18 of **Super Signals × AIDY — Provider Intelligence Master Build & 21-Day Builder**.

## Actual Vantage / MT5 account-mode proof

The existing connected Vantage MT5 account was **not reconfigured**. Day 18 only needed to prove whether the already-connected account behaves as hedging or netting.

Immutable `broker_deals` prove **hedging-mode behaviour** on the same live account:

- SELL broker position `1956449478` opened `2026-09-08 08:09:50.358Z`; no exit deal existed when checked.
- SELL broker position `1956449557` opened `2026-09-08 08:09:50.644Z`; no exit deal existed when checked.
- BUY broker position `1957234939` then opened `2026-09-08 09:47:30.083Z` and closed `09:51:20.661Z`.
- BUY broker position `1957235187` then opened `2026-09-08 09:47:32.145Z` and closed `09:51:20.667Z`.

Opposite XAUUSD directions therefore coexisted as distinct broker position IDs. A netting account would collapse opposing exposure into a single net position. This is direct broker-behaviour evidence that the current account supports hedging semantics.

One local `positions.closed_at` timestamp was inconsistent, so it was explicitly rejected as proof; acceptance uses immutable broker deal entry/exit records instead.

## Harness built

- explicit hedging / netting / unknown account-mode model;
- netting collapse semantics for opposite XAUUSD positions;
- hedging-mode per-provider broker-position attribution;
- net and gross XAUUSD heat via Day 17 allocator;
- provider/relay-cluster aggregation (one strongest eligible candidate per cluster/side for research combination);
- explicit horizon compatibility using overlapping expected-duration intervals;
- dormant `buy_only`, `sell_only`, `both`, `reduced`, `none` research choices;
- literal `both` branch only when hedging mode is proven **and** opposing horizons are compatible;
- unknown mode fails flat;
- no production portfolio authority.

## Acceptance

Day 18 engineering GREEN requires:

1. netting-mode tests collapse opposing positions and block literal hedge logic;
2. hedging-mode tests preserve per-provider attribution;
3. same-cluster relay copies do not multiply evidence;
4. incompatible horizons block the `both` branch;
5. unknown account mode fails flat;
6. gross and provider-cluster heat still constrain apparent hedges;
7. the actual connected Vantage account mode is proven from broker truth;
8. `PRODUCTION_PORTFOLIO_AUTHORITY = False`.

Statistical portfolio authority is **not** granted by Day 18 engineering acceptance. The combined-book engine remains dormant/research-only.