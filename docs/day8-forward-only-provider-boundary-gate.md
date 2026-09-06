# Day 8 — Forward-Only Provider Learning Boundary + Legacy Closure

Date: 2026-09-06
Status: implementation gate

## Purpose

Day 8 makes the Day 7 immutable provider-profile ledger operationally authoritative for provider research and AI semantic context. A historical/backfilled signal may use only provider knowledge whose profile version was already effective when that signal was posted.

## Boundary

- Super Signals owns Telegram/provider profile history and Provider Lab research.
- AIDY remains independent point-in-time Gold market/context truth.
- No broker/member execution authority is added or changed.
- No formal-forward authority is added.
- Current mutable provider profiles remain a cache for present-day refresh/build work, not a historical truth source.

## Forward-only rules

1. Resolve provider learning by `source_id` and `signal/message timestamp` against `provider_research_profile_versions`.
2. Require `profile.effective_at <= evaluation_timestamp`.
3. Never fall back to `provider_research_profiles` for historical/replay context.
4. Never rebuild an adaptive profile from current/full history while interpreting an old message.
5. Store exact provider profile version provenance on each newly enrolled research trade.
6. A timestamp before the Day 7 bootstrap origin is `legacy_unresolvable`; no profile is fabricated or backdated.
7. A `legacy_unresolvable` research trade can remain as historical evidence, but it can never be fair-score eligible.
8. Database constraints and a provenance trigger enforce the score/provenance boundary even if an application path regresses later.

## Legacy closure baseline

Immediately before Day 8 implementation, production contained 144 shadow/research trades and all 144 predated their source's Day 7 immutable profile origin. Six were still marked score-eligible. Day 8 must quarantine those unprovable score rows instead of pretending the current provider profile was known in the past.

These counts are a pre-migration observation, not a permanent constant; production acceptance must re-query the real database after rollout.

## Acceptance

Day 8 cannot be called complete until:

- `0057_provider_pit_boundary` is the live migration head;
- full production API/web/security quality gates pass;
- every research trade has explicit PIT status;
- zero `legacy_unresolvable` trades are score-eligible;
- every `resolved` trade references an exact profile version for the same source with `effective_at <= signal_posted_at`;
- pre-Day-7 timestamps resolve no provider profile rather than current state;
- provider AI context no longer reads mutable current profiles for historical messages;
- new enrollment stamps immutable provider version provenance;
- Provider Lab/AIDY resolver remains healthy;
- live broker/member execution code is unchanged by the Day 8 diff;
- AIDY source/runtime is unchanged unless separately justified;
- Memory is updated, validation passes, the Memory PR is merged, and merged Memory `main` is re-read.
