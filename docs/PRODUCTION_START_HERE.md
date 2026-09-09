# SUPER SIGNALS + AIDY — PRODUCTION START HERE

**This is the only production build/runtime authority.** Day-numbered build notes, historical Notion pages, migrations, old audit rankings, backup branches and superseded handoffs are evidence/history only. They must never be used to decide what production should do.

When asked to "check the build", "check what is live", "fix production", or "what rule are we using", start here and verify the current production branch/runtime. Do not infer current behaviour from an older Day page or migration comment.

## Canonical repository and refs

- Repository: `dannythehat/super-signals`
- Canonical code refs: `main` and `production`
- Temporary Render deployment alias: `feature/day-10-shared-telegram-sources`
- The three refs must point at the same accepted production SHA until Render is switched from the temporary alias to `production`.
- Pre-stabilisation snapshot: `backup/pre-stabilisation-20260909-ade297e`
- Previous stale-main snapshot: `backup/main-pre-stabilisation-20260909-bfa4d8b`

## Canonical Render runtime

- Service: `super-signals-day-8`
- Service ID: `srv-d9qmcgks728c73a555m0`
- Region: Frankfurt
- Runtime: Docker
- Health path: `/health`
- Auto-deploy: enabled

The service name is historical only. It does **not** mean Day 8 code is the production architecture.

## One production trading chain

There is one production entrypoint and one intended execution chain:

`app.main`
→ `app.production_listener.build_production_listener_manager`
→ `app.telegram_listener_canonical.CanonicalProductionTelegramListenerManager`
→ provider-aware AIDY/AI interpretation
→ canonical stored decision
→ `app.execution_router_canonical`
→ MetaAPI
→ Vantage MT5
→ PostgreSQL/broker reconciliation
→ app + notifications

No day-numbered listener builder may be selected from `app.main`. A historical module may remain temporarily as an implementation dependency while the runtime is flattened, but it is not an alternate production entrypoint and it may not own provider policy.

## Single live provider-policy authority

Current provider execution/risk rules live only in:

`services/api/app/provider_risk_policy.py`

Policy generation:

`owner-authority-2026-09-09-v1`

Historical migrations, provider rankings, old tests and build notes cannot override this module.

### Current locked rules

- **FXTradingVision — BUY:** TP1 5%, TP2 5%, TP3 1%.
- **FXTradingVision — SELL:** TP1 5%, TP2 5%, TP3 1%.
- **TIG’s Asia Trades — BUY:** enabled, 1% per atomic provider position/leg.
- **TIG’s Asia Trades — SELL:** enabled, 1% per atomic provider position/leg.
- **TIG:** no hidden direction veto and no historical TP cap.
- Other provider-specific restrictions remain exactly as encoded in `provider_risk_policy.py` until the owner explicitly changes them.

Any future provider/risk change must be made in this single policy module and protected by regression tests in the same change.

## Telegram reliability invariants

- Raw Telegram evidence is committed before slow downstream processing.
- Same-provider processing is ordered.
- Duplicate/reconnect delivery is idempotent.
- A fresh edited message that arrives before its original must not be silently dropped.
- Recovered stale market entries must never be force-executed late.
- Management instructions must remain first-class actions.

## AIDY boundary

Super Signals has one production decision/execution doorway. AIDY/Provider Lab research, historical replay and market-data workers may inform provider intelligence, but they do not get a second broker execution path.

AIDY-related experimental/test endpoints are research dependencies unless explicitly promoted and recorded here. A failure in Provider Lab historical market replay must not block the live Telegram → broker chain.

## Release gate

A production change is accepted only when all of the following are true:

1. Current provider-policy regression tests pass.
2. Production-listener wiring tests pass.
3. Full API/web build passes.
4. Render deploy for the exact SHA becomes `live`.
5. `/health` is healthy.
6. Trading-critical changes are confirmed by broker/audit evidence or the next genuine provider event when live-event proof is required.
7. `main`, `production`, and the temporary Render alias are aligned to the accepted SHA.

## Rule for cleanup

Do not delete a historical runtime module merely because its filename is old if production still imports it indirectly. First flatten the required behaviour into the canonical module, add an absence/regression test, prove the build, then delete the superseded file. Git history is the archive; dead production code does not need to remain in the runtime forever.
