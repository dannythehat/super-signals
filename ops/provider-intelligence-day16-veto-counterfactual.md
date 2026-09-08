# Provider Intelligence Day 16 — AIDY veto/filter counterfactual

## Scope

Day 16 supplies the missing preregistered accept/reject counterfactual harness beneath Days 17 and 18.

- Decision evidence must be preregistered and have an evidence cutoff at or before the signal timestamp.
- Research reject requires the inherited Day-13 OOS-N floor, minimum-effect gate and BH/FDR rejection.
- The decision is persisted before outcome resolution.
- Outcome resolution compares execution-cost-adjusted raw baseline R with the filtered counterfactual.
- Rejected losers record avoided loss; rejected winners record sacrificed winner R.
- Skipped/rejected trades never authorize a Telegram broadcast.
- The entire subsystem is research-only and non-executable.

## Authority

Engineering/causal bookkeeping may GREEN.

Veto efficacy and all live/paper veto authority remain `WAITING-FOR-FORWARD-EVIDENCE` until sufficient genuine forward OOS evidence exists. Building the harness does not approve the veto.
