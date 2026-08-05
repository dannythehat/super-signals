# Super Signals

Private Telegram signal aggregation and automated Vantage MT5 trading platform.

> **Status:** Foundation build. Demo and test environments only. No external user may trade live until the technical, security and Europe-focused legal launch gates have passed.

## Product purpose

Super Signals combines approved Telegram signal sources into one controlled system. Recognised trade messages are filtered from ordinary conversation, recorded as a single internal signal event, published to the private Super Signals channel, shown in the mobile-first app and sent to eligible users' connected Vantage MT5 accounts.

## Version-one scope

- Private, invitation-only web app/PWA
- Owner Admin, Trading Admin and invited User roles
- Multiple authorised Telegram reader accounts per administrator
- Explicit selection of approved source groups and channels
- Testing, Live and Paused source states
- Strict trade/update/chatter classification
- Exact parser configuration per approved provider
- Clean branded Super Signals channel posts without source identity
- XAUUSD BUY and SELL automation
- One separate MT5 position for every take-profit level
- 0.5% or 1% risk per position, including explicit double-size instructions
- One-time entry-price check and all-or-nothing funds validation
- Follow-up trade management instructions
- Mobile dashboard, signal timeline, performance and notifications
- Append-only audit history and broker reconciliation

The complete functional rules are maintained in the private Notion working blueprint and the repository scope document.

## Planned repository structure

```text
apps/
  web/                 React/TypeScript mobile-first PWA
services/
  api/                 FastAPI application and authentication
  telegram/            Telegram sessions, listeners and message routing
  trading/             MT5 execution, monitoring and reconciliation
packages/
  shared/              Shared schemas, types and validation rules
docs/                   Scope, decisions, security and operating procedures
.github/                Pull request and automated workflow controls
```

The folders are introduced during Day 2 when the frontend and backend are scaffolded.

## Build workflow

1. `main` is the source-of-truth branch.
2. Work is completed on a named branch.
3. Changes are reviewed through a pull request.
4. Tests and checks must pass before merge.
5. Production deployment will eventually run only from `main`.
6. Every build day records its commit, checks and evidence in the Notion build calendar.

See [docs/WORKFLOW.md](docs/WORKFLOW.md).

## Security rule

**Never commit credentials, tokens, session files or private keys.**

This includes Telegram sessions and API credentials, Vantage/MT5 credentials, MetaAPI tokens, database URLs, Cloudflare tokens, encryption keys and application secrets. See [SECURITY.md](SECURITY.md).

## Licensing

This is private proprietary software. No open-source licence is granted.
