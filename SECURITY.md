# Security Policy

Super Signals handles highly sensitive Telegram and trading-account access. Security controls are part of the product, not optional finishing work.

## Never commit secrets

The repository must never contain real values for:

- Telegram API ID or API hash
- Telegram login codes, QR tokens or authorised session files
- Telegram publishing-bot tokens
- Vantage/MT5 account numbers, passwords or server credentials
- MetaAPI or other MT5 integration tokens
- Database connection strings
- Cloudflare API tokens
- Encryption keys, JWT secrets or recovery keys
- User personal data or broker statements

Use local environment variables during development and encrypted hosting secrets after deployment. Only placeholder values belong in `.env.example`.

## Sensitive Telegram sessions

A Telegram user session can act with the connected account's existing access. Session material must therefore be:

- encrypted at rest
- unavailable in ordinary admin screens
- excluded from application logs
- isolated by account owner
- revocable through both Super Signals and Telegram Devices
- destroyed when the connection is deliberately removed

Dedicated signal-reader accounts are preferred over personal Telegram accounts.

## Trading credentials

Trading credentials must be encrypted and restricted to the minimum service that requires them. They must never be displayed back to administrators after storage.

## Reporting a security concern

Do not open a public issue containing credentials, screenshots of secrets or exploitable details. Contact the repository owner privately and rotate any potentially exposed credential immediately.

## Incident rule

When a credential may have been exposed:

1. Disable or rotate it immediately.
2. Stop the affected service where required.
3. Review logs without copying the secret into tickets or chat.
4. Record the event and remediation in the private audit trail.
5. Verify that the old credential no longer works.
