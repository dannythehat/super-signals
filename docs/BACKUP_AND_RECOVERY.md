# Database backup, recovery and retention

Backups are useful only when restoration is proved. Super Signals therefore separates **operational record retention** from **backup recovery points** and requires isolated restore evidence before the live pilot.

## Day 39 live restore proof — 13 August 2026

The active Render PostgreSQL 18 database was backed up from an exported `REPEATABLE READ / READ ONLY` PostgreSQL snapshot using PostgreSQL 18 `pg_dump` custom format. The same exported snapshot was used for the reference counts/digests and the dump, so normal Telegram/runtime writes could not create a false mismatch during acceptance.

The dump was restored into a randomly named isolated scratch database on the same managed PostgreSQL instance. Acceptance then proved:

- Alembic revision matched the source snapshot.
- **41 public tables / 18,414 snapshot rows** restored with identical per-table counts.
- Canonical identity digests for `messages`, `signals`, `positions` and `audit_events` matched the source snapshot.
- Both append-only `audit_events` triggers were present and enabled after restore.
- A real attempted UPDATE against the restored audit log was rejected by the append-only trigger.
- The scratch database was dropped after verification.
- The temporary dump existed only in the acceptance container's ephemeral `/tmp` filesystem and was deleted after the proof.
- No Telegram or broker trade action was created by the backup test.

The immutable acceptance event is `day39.backup_restore_verified` in PostgreSQL. It stores only safe metadata/digest evidence, never database credentials or backup contents.

## Recovery objectives

For the private friends-and-family tool:

- Recovery point objective: **24 hours maximum** once ordinary live-member trading begins.
- Recovery time objective: **4 hours**.
- A backup/restore failure is a launch blocker, not a warning to ignore.

## Required backup layers before Day 41 live pilot

1. **Durable managed PostgreSQL** — the database used for the live pilot must not be an expiring/free preview database.
2. **Provider-managed recovery** — enable the backup/PITR capability available on the selected durable database plan.
3. **Independent encrypted logical backup** — at least daily, stored outside the primary database provider/deletion boundary.
4. **Pre-migration logical backup** — take a verified backup immediately before a live schema migration.
5. **Restore verification** — regularly restore into an isolated database and verify schema, canonical row identities and append-only audit protection before discarding the scratch copy.
6. **Broker decrypt-key recovery** — database ciphertext is useless without a valid broker credential decryption key. Preserve the active key set separately from PostgreSQL, source control and the backup itself.

## Seven-year operational record retention

The Working Blueprint requires Super Signals operational records to remain available for seven years. Day 39 locks the implementation design:

- Canonical Telegram evidence, message revisions, AI/deterministic decisions, Signals, lifecycle events, mapped positions, immutable broker deals/performance history, notifications and audit events are **not automatically purged** during their seven-year retention window.
- PostgreSQL remains the working source of truth while the dataset is operationally practical.
- If older records later need to move out of the hot database, they may be archived only into encrypted owner-controlled object/archive storage with an integrity manifest and documented restore procedure. Archiving must not destroy the canonical linkage required to reconstruct a Signal/trade history.
- Keep a **year-end encrypted archive snapshot for at least seven years**. This is in addition to rolling operational backups; it is not a substitute for daily recovery points.
- Recommended rolling recovery set once live: 7 daily, 4 weekly and 12 monthly independent encrypted backups, plus the seven-year annual archive series.
- Audit events remain append-only before and after any archive/restore operation.
- Expiration/deletion jobs must default to fail-closed. No generic cleanup job may delete canonical trading/audit history merely to reduce database size.

The seven-year rule applies to **records and reconstructable history**, not to retaining every daily backup for seven years.

## Broker credential recovery boundary

The active MT5 decrypt key set is held in deployment secrets, never in Git, Notion, logs, screenshots or PostgreSQL.

- `SUPER_SIGNALS_BROKER_CREDENTIAL_KEYS` is the primary active key set.
- `SUPER_SIGNALS_MT5_ENCRYPTION_KEYS` is the fallback compatibility key set where still configured.
- Before the live pilot, at least one separate owner-controlled password-manager/secret-manager recovery copy of the active key set must exist.
- Never put plaintext broker decrypt keys in the backup archive itself.

See `docs/MT5_METAAPI_OPERATIONS.md` before rotating or replacing broker credential keys.

## Production restore procedure

1. Stop API, Telegram ingestion and broker write paths.
2. Record the incident and selected recovery point.
3. Preserve a final snapshot of the damaged database when safe.
4. Restore into a **new isolated database**, never over the only remaining copy.
5. Verify Alembic revision, all critical table counts, role assignments, canonical Signal/position identities and audit triggers.
6. Prove the restored audit log still rejects UPDATE and DELETE.
7. Restore the broker decrypt key set from the independent secret manager.
8. Rotate database/application credentials if compromise is possible.
9. Point the application to the restored database only after verification.
10. Run health, Telegram plan reconstruction and MT5 reconciliation before resuming broker writes.
11. Confirm restart/replay does not create a new Position/publication for already processed Signals.
12. Retain incident evidence and document any data-loss window.

## Current infrastructure blocker recorded by Day 39

As inspected on **13 August 2026**, the current Render PostgreSQL resource is a **free preview database** with expiry **6 September 2026** and its external IP allow-list is `0.0.0.0/0` (`everywhere`). It was sufficient for the isolated Day 39 restore proof, but it is **not acceptable as the durable Day 41 live-pilot database**.

Before Day 41:

- move/upgrade to a non-expiring durable PostgreSQL resource with appropriate managed recovery;
- restrict public database network access to the minimum required, ideally using Render's private/internal connectivity for the web service and no broad external allow-list;
- repeat the Day 39 restore proof against the durable target after migration;
- verify the permanent backup destination is separate from the primary database deletion boundary.

No paid Render plan change is performed automatically by the codebase. The Owner must deliberately approve the infrastructure plan before the live pilot.
