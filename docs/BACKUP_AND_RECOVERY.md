# Database backup and recovery

Backups are only useful when restoration is tested. Super Signals therefore uses both provider-managed recovery and independent logical backups before any live trading data is accepted.

## Recovery objectives

Private beta targets:

- Recovery point objective: 24 hours
- Recovery time objective: 4 hours

These targets must be tightened before broader or live-trading use if operational volume requires it.

## Required backup layers

1. **Managed database recovery** — automated backups and point-in-time recovery must be enabled where the selected plan supports them.
2. **Independent logical backup** — encrypted `pg_dump` output stored outside the database provider.
3. **Pre-migration backup** — create a verified backup immediately before a production schema migration.
4. **Audit preservation** — backup and restore must retain append-only audit rows and their mutation-blocking triggers.
5. **Broker decrypt-key recovery** — a database backup containing encrypted MetaAPI tokens is useless without at least one valid broker credential decryption key. The active key set must therefore be preserved separately from PostgreSQL and source control.

## Broker credential recovery boundary

The active MT5 decrypt key set is held in deployment secrets, never in Git, Notion, logs, screenshots or the database.

For the controlled preview service:

- `SUPER_SIGNALS_BROKER_CREDENTIAL_KEYS` is the primary active key set.
- `SUPER_SIGNALS_MT5_ENCRYPTION_KEYS` is maintained as a fallback copy so accidental removal of the primary variable does not immediately make stored MetaAPI credentials unreadable.
- both variables must be treated as permanent infrastructure secrets, not temporary Day 22 credentials.

Before external launch, the same active key set must also have a separate owner-controlled secret-manager/password-manager recovery copy. Never put the plaintext key into a project document or support ticket.

See `docs/MT5_METAAPI_OPERATIONS.md` before rotating or replacing broker credential keys.

## Retention baseline

Before external launch, retain at minimum:

- 7 daily backups
- 4 weekly backups
- 12 monthly backups

Backup storage must use encryption at rest, restricted service credentials and a separate deletion boundary from the primary database.

## Automated restore test

`scripts/verify_database_backup.sh` performs the CI acceptance test:

1. upgrades a clean PostgreSQL database to the latest migration
2. seeds the non-secret test owner
3. creates a custom-format logical backup
4. restores it into a separate empty database
5. verifies all critical tables, role seeds, owner seed, Alembic version and audit protection triggers
6. deletes the disposable restore database

The test contains no production data or secrets.

## Production restore procedure

1. Stop API, Telegram and trading writes.
2. Record the incident and selected recovery point.
3. Create a final snapshot of the damaged database when safe.
4. Restore into a new isolated database, never over the only remaining copy.
5. Validate migrations, row counts, role assignments, audit triggers and a sample of signals and positions.
6. Restore the broker credential decrypt key set from the deployment secret manager before attempting MT5 reconciliation.
7. Rotate database credentials if compromise is possible.
8. point the API to the restored database through the secret manager.
9. run health and MT5 reconciliation checks before resuming writes.
10. retain incident evidence and document any data-loss window.

## Current managed environment

The controlled Render preview PostgreSQL database now contains the accepted Day 22 encrypted MetaAPI-backed MT5 demo connection. Live trading remains disabled. A separate production database and production-only secret set remain launch requirements.