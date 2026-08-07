# Secure Telegram connection

Day 8 adds the administrator-side Telegram authorisation boundary. It does not yet select groups, ingest messages or place trades.

## Scope

An Owner Admin or Trading Admin with `sources.manage` may:

1. start a short-lived Telegram QR authorisation flow
2. scan the QR using an already authorised Telegram mobile application
3. provide Telegram's two-step verification password when Telegram requests it
4. save the resulting Telethon `StringSession` encrypted in PostgreSQL
5. verify the encrypted session after the API service restarts
6. disconnect the account and destroy the saved server session

Invited users cannot access these routes.

## Server secrets

Configure these only in the API service's encrypted secret store:

- `TELEGRAM_API_ID`
- `TELEGRAM_API_HASH`
- `SUPER_SIGNALS_TEGRAM_SESSION_KEYS`
- `SUPER_SIGNALS_TELEGRAM_QR_TTL_SECONDS` (optional, defaults to 120)

`SUPER_SIGNALS_TELEGRAM_SESSION_KEYS` is a comma-separated Fernet key ring. The first key encrypts new sessions. Following keys remain available only to decrypt older ciphertext during a controlled rotation.

Generate a key locally:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Never put Telegram credentials, QR URLs, session strings or encryption keys in browser `VITE_` variables, source control, logs, pull requests or chat messages.

## API flow

All routes use the authenticated session cookie and require `sources.manage`.

```text
POST /admin/telegram/accounts/authorize
GET  /admin/telegram/accounts/authorize/{flow_id}
POST /admin/telegram/accounts/authorize/{flow_id}/password
GET  /admin/telegram/accounts
POST /admin/telegram/accounts/{account_id}/verify
POST /admin/telegram/accounts/{account_id}/disconnect
```

The authorisation response contains Telegram's short-lived `tg://login` URL. The response is marked `Cache-Control: no-store`. The QR URL is never persisted or written to the audit log.

Pending QR flows live only in the API process and expire automatically. During Day 8, the QR flow therefore requires a single API process or sticky routing. Only a completed encrypted session survives a service restart. Multi-process coordination is a later reliability concern and must be solved before production source listening.

## Persistence and privacy

The database stores:

- administrator owner ID
- administrator-provided account label
- Telegram phone number for identity matching
- encrypted Telethon session ciphertext
- a SHA-256 session fingerprint used only to reject the exact same session twice
- connection status and last verified time

The API returns only a masked phone hint. Audit events contain the label, masked phone hint and Telegram numeric user ID, but never the portable session, QR URL, two-step password or full phone number.

## Restart verification

A new service instance decrypts the saved session, connects to Telegram and confirms that:

- the session remains authorised
- Telegram returns the same phone identity originally stored

An invalid or mismatched session is marked revoked and the original server session ciphertext is destroyed.

## Disconnect semantics

Disconnect first attempts Telegram `log_out()` to revoke the remote authorisation. Whether or not Telegram is reachable, the local portable session is irreversibly replaced with a random encrypted destruction marker and its original fingerprint is replaced. The account row remains with status `disconnected` so future source relationships and audit history are not cascade-deleted.

A disconnected account cannot be verified or used until it is authorised again.

## Day 8 acceptance evidence

Automated PostgreSQL integration tests prove that:

- only an authorised administrator can start the connection flow
- the QR token is not cached or audited
- the portable session is encrypted at rest
- the raw session and full phone number do not appear in audit payloads
- a fresh service instance can decrypt and verify the saved session
- Telegram two-step passwords are not echoed or stored
- Disconnect revokes remotely when possible and always destroys the original local session
- invited users receive an audited permission denial

The repository gate also reruns the complete API and frontend suites, dependency audits, production web build and isolated database backup/restore check.

The final live acceptance check still requires server-side Telegram API credentials and a dedicated test Telegram account to scan the QR. Do not use a personal production account for development acceptance.
