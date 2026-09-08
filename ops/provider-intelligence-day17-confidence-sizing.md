# Provider Intelligence Day 17 — Confidence calibration + dormant sizing

Date: 2026-09-08

Canonical Notion contract: Day 17 of **Super Signals × AIDY — Provider Intelligence Master Build & 21-Day Builder**.

## Scope built

- Confidence reliability bins suitable for reliability diagrams/calibration tables.
- Brier score, expected calibration error and maximum-bin-gap reporting.
- Proposed future 0.2%–2.0% risk-envelope mapping as a **research-only, non-executable** artifact.
- Net directional XAUUSD heat, gross XAUUSD heat and provider-cluster heat summaries.
- Direction-aware research allocator that still constrains apparent hedges by gross and cluster heat.
- Current execution sizing guard: accepted live/paper trades stay on one caller-supplied flat size; rejected/skipped trades are zero.

## Safety / authority state

- `VARIABLE_SIZING_AUTHORITY = WAITING-FOR-FORWARD-EVIDENCE`
- `LIVE_VARIABLE_SIZING_ALLOWED = False`
- `PAPER_VARIABLE_SIZING_ALLOWED = False`
- Calibration thresholds in this harness are explicitly `PROPOSED_UNAPPROVED` and cannot grant authority.
- There is deliberately no execution hook and no parameter that can make current live/paper sizing confidence-dependent.

## Engineering acceptance

Day 17 engineering acceptance requires tests proving:

1. reliability/calibration tables are deterministic;
2. insufficient forward N remains WAITING;
3. low vs high confidence produces identical current live/paper size;
4. rejected/skipped trades remain zero size;
5. future 0.2%–2.0% envelope math stays non-executable;
6. net, gross and provider-cluster heat are all enforced by the research allocator;
7. an apparent opposite-direction hedge may reduce net heat but cannot bypass gross heat;
8. existing books already outside a cap fail flat.

**Statistical authority is not part of Day 17 engineering GREEN.** Variable sizing remains WAITING until future forward calibration evidence and preregistered authority gates are satisfied.
