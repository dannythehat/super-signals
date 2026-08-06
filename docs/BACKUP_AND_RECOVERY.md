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
6. Rotate database credentials if compromise is possible.
7. point the API to the restored database through the secret manager.
8. run health and reconciliation checks before resuming writes.
9. retain incident evidence and document any data-loss window.

## Current managed environment

The preview Supabase database is connected and healthy. It contains foundation schema only and no live user, Telegram or broker data. A separate production database remains a launch requirement.
