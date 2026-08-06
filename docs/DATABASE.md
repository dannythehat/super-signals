# PostgreSQL core data model

Super Signals uses PostgreSQL for identities, permissions, Telegram metadata, original messages, structured signals, positions and immutable audit history.

## Tables

- `users`, `roles`, `user_roles`, `permissions`, `role_permissions` — private users and centrally enforced permissions
- `invitations` — hashed one-time access keys tied to an approved email
- `auth_sessions`, `password_recovery_requests` — hashed authentication and recovery tokens
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

Preview currently uses the connected Supabase project `super-signals-preview`. Production must use a separate managed PostgreSQL database and separate `DATABASE_URL` secret.

The selected production service must provide TLS, encryption at rest, restricted credentials, automated backups and point-in-time recovery where available. Do not commit a real connection string. Store it only in the deployment platform's encrypted secret manager.

## Migration and backup safety

CI starts a clean PostgreSQL 16 service, applies all migrations, verifies seeds and constraints, confirms `audit_events` cannot be changed or deleted, safely tests downgrade and reapplication, creates a custom-format logical backup and restores it into a separate database.

The restore gate verifies critical tables, the three role seeds, owner seed, migration marker and both audit-protection triggers. See [BACKUP_AND_RECOVERY.md](BACKUP_AND_RECOVERY.md).
