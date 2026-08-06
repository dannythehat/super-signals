# Foundation threat checklist

Super Signals can eventually place financial trades, so a control is not considered complete merely because the interface hides an action.

## Assets requiring strongest protection

- administrator and invited-user identities
- session tokens and recovery records
- Telegram user sessions and publishing tokens
- MT5 account credentials and broker integration tokens
- encryption keys
- original signal messages and parsed instructions
- positions, performance and reconciliation records
- append-only audit history

## Foundation checklist

| Threat | Foundation control | Status |
| --- | --- | --- |
| Credential committed to Git | `.gitignore`, security policy, placeholders only | Implemented |
| Known fallback secret used outside development | API now rejects missing or weak production configuration | Implemented |
| Account enumeration | login and recovery use generic responses | Implemented |
| Stolen browser token | random server-side token, hashed storage, HttpOnly and SameSite cookie | Implemented |
| Expired or revoked session reused | database expiry and revocation checks | Implemented |
| Role escalation | central database-backed permission matrix in API and interface | Implemented |
| Forbidden action hidden but callable | route-level permission dependency returns audited 403 | Implemented |
| Audit history edited | database update and delete triggers reject mutation | Implemented |
| Duplicate Telegram message or position | database uniqueness constraints | Implemented foundation |
| Ambiguous signal interpreted as a trade | unknown input must be skipped, never guessed | Locked rule; parser not built |
| Malicious Telegram content | treat all message content as untrusted data | Required in Telegram phase |
| Telegram session theft | encrypted storage, service isolation, revocation and log redaction | Required before connection |
| Broker credential theft | encrypted storage limited to trading service | Required before connection |
| Unauthorised live trading | explicit environment gate, emergency stop and no live action in foundation | Live trading disabled |
| Dependency compromise | lockfile, CI audit and Dependabot configuration | Implemented |
| Database loss | logical backup and full restore test in CI | Implemented foundation |
| Backup disclosure | encrypted separate storage with limited credentials | Required before production data |
| Brute force or request flooding | rate limiting and alerting | Required before external access |
| Administrator compromise | passkey or 2FA activation and recovery controls | Required before live access |
| Provider outage | health checks, reconciliation and recovery runbook | Required in service phases |

## Non-negotiable live gates

Live trading remains prohibited until all of the following are verified:

- hosted API and background services use isolated production secrets
- a separate production PostgreSQL database and tested backup destination exist
- Telegram and MT5 credentials are encrypted with managed key rotation
- administrator 2FA or passkeys are active
- login, recovery and sensitive endpoints are rate limited
- emergency stop is tested end to end
- broker reconciliation detects missing or manually changed positions
- legal, privacy and provider-permission reviews are complete

## Review cadence

Review this checklist when a new external system, credential type, privileged action or data category is introduced. A failed security check blocks merge unless the issue is explicitly recorded as a later-phase dependency and cannot expose current users or funds.
