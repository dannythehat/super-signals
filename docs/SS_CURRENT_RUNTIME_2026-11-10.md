# Super Signals: current production wiring (10 November 2026)

This is the operational architecture, not a proposal to start additional systems.

## One trading authority

- Owner demo master: **PU Prime MT5 700207861**, running on Contabo Windows server `VMI3693882`.
- Provider ingress and publication: existing **Render service `super-signals-day-8`**, branch `feature/day-10-shared-telegram-sources`, receives Telegram messages and serves the authenticated recent provider feed. Render is **not** a broker executor.
- Contabo master worker: `SS-Owner-Demo-Master-700207861`, `owner_demo_master_exec.py`.
- Follower copier: `SS-Owner-Demo-Copier-700207861`, `unified_copier.py`.
- Owner Telegram relay: `SS-Owner-Master-Telegram-700207861`, `owner_master_telegram_relay.py`.
- Account/P&L reporter: `SS-Owner-Demo-Account-Reporter-700207861`, broker-verified five-minute snapshots.
- Operational admin: `SuperSignals-Chat-Admin` and `SuperSignals-ReadOnly-GitHub-Bridge`.
- Preserve `SuperSignals-Demo-MT5` and `SuperSignals-Balance-Snapshot` unless their current usage has been proven irrelevant. Do not stop MT5 terminals merely because their process names are generic.

The only accepted automated broker execution is on the above Contabo MT5 master and its explicit follower copier. Do not enable Render or remote vendor execution.

## Permanently retired

- **MetaAPI vendor transport**: no use of any AgiliumTrade/MetaAPI provisioning, read, margin or trading endpoint.
- Previous Vantage-to-PU Prime live mirror and legacy Render-based broker settlement and watchdogs.
- Old Gina follower/watchdog, duplicate copier/watchdog and 9 November one-off master-login/diagnostic scheduled tasks.
- Removed one-off scripts and retired launchers are not to be resurrected as a workaround for a failed trade.

## Operating rules

1. Check broker-confirmed master positions first, followed by follower confirmations and Telegram delivery. A Windows task marked `Running` is not evidence of a working trade path.
2. A failed provider feed must surface as degraded or disconnected. Never fabricate an empty signal list or pretend that missing posts mean no provider activity.
3. Never replay historic provider messages or place synthetic orders to prove connectivity.
4. Never change the master/follower lot size or risk settings during an infrastructure repair.
5. Preserve all broker journals, trade history, provider allowlists, credentials and execution receipts.
6. Keep the vendor-resurrection CI test and fail closed if any obsolete vendor environment variable is set.
7. Deployment requires passing API and web CI, an explicit Render deployment, a successful authenticated Contabo feed test and checks for HTTP 502 plus genuine Telegram delivery.

Historical docs may mention retired systems as evidence. They are not active operating instructions.
