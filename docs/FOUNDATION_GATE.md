# Day 7 foundation pass gate

This gate must pass before Telegram implementation begins.

## Acceptance evidence required

- [ ] full TypeScript, Python, lint, format, test and production-build CI is green
- [ ] npm production and development dependency audits are clean
- [ ] Python dependency audit is clean
- [ ] database migration, logical backup and isolated restore test passes
- [ ] Cloudflare preview deploys from the pull request and returns the Super Signals app
- [x] architecture and trust boundaries are recorded
- [x] local, CI, preview and production environments are mapped
- [x] backup and recovery plan is recorded
- [x] foundation threat checklist is recorded
- [x] unresolved launch dependencies are recorded

The unchecked technical evidence is completed by the Day 7 pull-request workflows. Links and final counts are added to the pull request and Notion evidence before merge.

## Foundation issues corrected during review

- Added a reproducible npm lockfile process.
- Upgraded vulnerable ESLint, Vite and Vitest development tooling.
- Added dependency auditing to CI.
- Replaced `npm install` with deterministic `npm ci` in controlled builds.
- Upgraded the official GitHub checkout, Node and Python actions to their current v7 releases.
- Removed obsolete one-day formatting workflows.
- Replaced the old Day 3-only preview deployment with pull-request preview deployment.
- Added fail-closed non-development configuration for database, CORS and session fingerprint secrets.
- Added a database backup and full restore acceptance test.

## Recorded dependencies, not permission to launch

The following work is intentionally scheduled for later phases and does not block beginning Telegram development:

- production API and background-service hosting
- separate production PostgreSQL database
- managed encryption and key rotation for Telegram and MT5 credentials
- administrator passkey or 2FA activation
- endpoint rate limiting and operational alerting
- Telegram account connection and approved source selection
- broker integration, emergency stop and reconciliation
- legal, privacy and source-redistribution approval gates

None of these items may be treated as optional before external or live-trading use.
