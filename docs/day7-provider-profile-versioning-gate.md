# Day 7 — Provider Identity, Style and Behavioural-Profile Versioning

Date: 2026-09-06
Status: implementation gate

## Purpose

Day 7 adds immutable point-in-time history around the Provider Lab intelligence that already exists. It does **not** rebuild provider discovery, the style classifier, adaptive grammar learning, AIDY market truth, deterministic replay, benchmark scoring or broker execution.

The mutable `provider_research_profiles` row remains the current-state cache used by existing services. A new append-only `provider_research_profile_versions` ledger records what provider identity/profile state became known, and when.

## Boundary

- Super Signals owns provider/source identity and Provider Lab profiles.
- AIDY remains the independent point-in-time market-truth provider.
- The databases remain separate.
- No new broker execution path is introduced.
- No formal-forward authority is introduced.
- Adaptive/profile evidence remains research/shadow-safe.

## Versioned state

Each version contains:

- stable `source_id`;
- Telegram `chat_id`;
- current chat title and source alias;
- source status;
- research state;
- provider style;
- observed/signal/management/edit counts;
- signal-likelihood and interpretation-readiness metrics;
- duplicate-provider evidence;
- profile metadata including `adaptive_v1` language and behavioural research.

The adaptive profile's `generated_at_epoch` is excluded from the semantic fingerprint. A periodic refresh that learns nothing new therefore creates no history row.

## Point-in-time rules

1. Existing profiles are bootstrapped only at the Day 7 migration time with `change_source=bootstrap_current_state`.
2. Bootstrap rows are not backdated and must never be represented as historical knowledge before Day 7.
3. Meaningful profile changes append a new monotonically increasing per-provider version.
4. Provider identity/status changes append immediately when a research profile exists.
5. Previous versions cannot be updated or deleted.
6. `provider_research_profile_version_as_of(source_id, timestamp)` returns only a version with `effective_at <= timestamp`.
7. No current/future profile state may be used to rewrite earlier Provider Lab evidence.

## Rollout acceptance

Before Day 7 can be called complete:

- branch tests and full production quality gates are green;
- migration chain is a single head from `0055_aidy_provider_lab_truth` to `0056_provider_profile_versions`;
- migration applies successfully in the real production database;
- every existing `provider_research_profiles.source_id` has at least one version after bootstrap;
- the version table contains no duplicate `(source_id, version_no)` values;
- a no-op adaptive refresh does not append a version solely because `generated_at_epoch` changed;
- a real identity/profile change appends exactly one new version and leaves the previous row unchanged;
- the as-of reader returns the older version for a timestamp between two versions;
- live AIDY capture/archive health remains green;
- broker/member execution behaviour is unchanged;
- Memory is updated, merged and re-read from `main` with the final evidence and next step.

Until all of those gates are proven, Day 7 remains **in progress**.
