# Provider Intelligence Day 10 — immutable per-signal AIDY context

## Objective

Persist exactly one immutable research context attachment per PIT-clean provider signal.
The attachment freezes the provider-profile version and independent AIDY market context
that were both knowable at the signal timestamp.

## Ownership boundary

- Super Signals owns provider identity/profile history and the durable joined research row.
- AIDY owns independent Gold market/session/regime context.
- The attachment is research evidence only.
- It cannot grant broker, member, publication or sizing authority.
- AIDY formal-forward authority remains independent and unchanged.

## Temporal contract

A row may be created only when all of these hold:

1. the provider profile is a Day-8 `resolved` immutable version for the same source;
2. provider profile `effective_at <= signal_posted_at`;
3. AIDY was requested with `as_of == signal_posted_at`;
4. AIDY context/snapshot time is `<= signal_posted_at`;
5. AIDY context lag is within the Day-9 bounded contract;
6. AIDY provenance is private-forward/research-only and explicitly denies live-money authority;
7. a matching resolved `shadow_trades` research anchor exists for the signal.

Legacy `legacy_unresolvable` signals are not backfilled and never become candidates.

## Persistence contract

`provider_signal_context_attachments` is append-only:

- one unique row per `signal_id`;
- exact signal/source/message identity;
- exact provider profile version identity;
- exact AIDY context hash and immutable snapshot identity;
- frozen session/regime/data-quality/market/provenance JSON;
- canonical full attachment payload plus SHA-256 digest;
- database trigger rejects UPDATE or DELETE;
- database validation trigger rechecks signal, profile and resolved research provenance.

Multiple entry legs from one signal therefore share one canonical context record rather
than duplicating the same context.

## Runtime isolation

The attachment resolver runs inside the existing application-owned AIDY Provider Lab
research runtime, after the M1 replay resolver and behind a separate exception boundary.

AIDY context unavailability or staleness:

- does not block signal enrollment;
- does not block M1 research resolution;
- does not mutate a real trade;
- does not call MetaAPI or member execution;
- retries on a later bounded research poll while the signal remains unattached.

## Acceptance

Day 10 engineering acceptance requires:

- migration chain upgrades cleanly from Day 8 head `0057_provider_pit_boundary`;
- full API regression passes on PostgreSQL 18;
- deterministic payload/digest tests pass;
- future-provider and future-AIDY leakage are database/application blocked;
- attachment UPDATE/DELETE are blocked;
- duplicate signal attachment is idempotent;
- candidate selection is bounded and resolved-only;
- runtime context failures are isolated from M1 research and execution;
- production deployment leaves broker/member execution semantics unchanged.

A fresh real forward signal attachment can be marked `WAITING` when no qualifying signal
exists during the verification window. It must not be fabricated from legacy data.
