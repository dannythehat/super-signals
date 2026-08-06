# PostgreSQL core data model

Day 4 introduces the first PostgreSQL schema for Super Signals.

## Tables

- `users`, `roles`, `user_roles` — private users and separated owner/trading-admin permissions
- `invitations` — hashed one-time access keys tied to an approved email
- `telegram_accounts`, `sources` — encrypted Telegram sessions and approved exact chats
- `messages`, `signals` — original Telegram messages and the parser's structured result
- `positions` — one row per user, signal and take-profit position
- `audit_events` — append-only security and trading audit history

Sensitive Telegram session material is represented only as encrypted ciphertext plus a non-secret fingerprint. Plain Telegram sessions, MT5 credentials and invitation keys must never be stored.

## Local database

```bash
docker compose up -d postgres
export DATABASE_URL=postgresql+psycopg://super_signals:super_signals@127.0.0.1:5432/super_signals
python -m alembic -c services/api/alembic.ini upgrade head
```

Seed the owner account with `services/api` on the Python module path:

```bash
PYTHONPATH=services/api SUPER_SIGNALS_OWNER_EMAIL=owner@example.com python -m app.seed
```

## Managed PostgreSQL

Production and preview must each use a separate managed PostgreSQL database and separate `DATABASE_URL` secret. The application supports any PostgreSQL 16-compatible provider with TLS. The provider must enable automated backups, point-in-time recovery where available, encryption at rest and restricted network credentials.

Do not commit a real connection string. Store it only in the deployment platform's encrypted secret manager.

## Migration safety

The CI job starts a clean PostgreSQL 16 service, upgrades to the latest migration, verifies the owner seed and uniqueness constraints, confirms `audit_events` cannot be changed or deleted, downgrades to an empty schema, and then reapplies the migration.
