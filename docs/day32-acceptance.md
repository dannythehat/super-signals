# Day 32 acceptance boundary

Day 32 replaces the infrastructure overview with the everyday mobile Super Signals home while preserving setup and administration under Settings.

The dashboard is read-only at the broker. It reuses the proven MetaAPI terminal reader, exposes only mapped Super Signals positions that still exist at the broker, and may reconcile stale local position flags to `closed / external_close` when broker truth proves they are absent. It has no broker trade gateway.

Performance cards on Day 32 are explicitly provisional and use only already-recorded position P/L. Day 33 owns the canonical broker-backed performance ledger and will replace the provisional aggregates rather than creating a second source of truth.

Private provider identity, Telegram IDs/raw text, credentials, manual MT5 positions and private account secrets are excluded from the dashboard response. The PWA service worker caches only the static shell/assets and never caches private API/account/admin/auth responses.
