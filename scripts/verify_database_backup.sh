#!/usr/bin/env bash
set -euo pipefail

: "${DATABASE_URL:?DATABASE_URL is required}"

export PGHOST="${PGHOST:-127.0.0.1}"
export PGPORT="${PGPORT:-5432}"
export PGUSER="${PGUSER:-super_signals}"
export PGPASSWORD="${PGPASSWORD:-super_signals}"
export PGDATABASE="${PGDATABASE:-super_signals_test}"

restore_database="${PGRESTORE_DATABASE:-super_signals_restore}"
backup_file="$(mktemp "${TMPDIR:-/tmp}/super-signals-backup.XXXXXX")"

admin_pg() {
  PGUSER="${PGADMINUSER:-$PGUSER}" \
    PGPASSWORD="${PGADMINPASSWORD:-$PGPASSWORD}" \
    "$@"
}

cleanup() {
  admin_pg dropdb --if-exists "$restore_database" >/dev/null 2>&1 || true
  rm -f "$backup_file"
}
trap cleanup EXIT

python -m alembic -c services/api/alembic.ini upgrade head
PYTHONPATH=services/api \
  SUPER_SIGNALS_OWNER_EMAIL=owner@super-signals.test \
  python -m app.seed

pg_dump \
  --format=custom \
  --no-owner \
  --no-privileges \
  --file="$backup_file" \
  "$PGDATABASE"

admin_pg dropdb --if-exists "$restore_database"
admin_pg createdb \
  --template=template0 \
  --owner="$PGUSER" \
  "$restore_database"
pg_restore \
  --no-owner \
  --no-privileges \
  --dbname="$restore_database" \
  "$backup_file"

required_tables=(
  alembic_version
  audit_events
  auth_sessions
  invitations
  messages
  password_recovery_requests
  permissions
  positions
  role_permissions
  roles
  signals
  sources
  telegram_accounts
  user_roles
  users
)

for table in "${required_tables[@]}"; do
  exists="$(psql --dbname="$restore_database" --tuples-only --no-align --command="SELECT to_regclass('public.${table}') IS NOT NULL")"
  if [[ "$exists" != "t" ]]; then
    echo "Restored database is missing table: $table"
    exit 1
  fi
done

role_count="$(psql --dbname="$restore_database" --tuples-only --no-align --command="SELECT count(*) FROM roles WHERE name IN ('owner', 'trading_admin', 'user')")"
owner_count="$(psql --dbname="$restore_database" --tuples-only --no-align --command="SELECT count(*) FROM users WHERE lower(email::text) = 'owner@super-signals.test'")"
migration_count="$(psql --dbname="$restore_database" --tuples-only --no-align --command="SELECT count(*) FROM alembic_version")"
audit_trigger_count="$(psql --dbname="$restore_database" --tuples-only --no-align --command="SELECT count(*) FROM pg_trigger WHERE tgrelid = 'audit_events'::regclass AND NOT tgisinternal")"

[[ "$role_count" == "3" ]]
[[ "$owner_count" == "1" ]]
[[ "$migration_count" == "1" ]]
[[ "$audit_trigger_count" == "2" ]]

echo "Database backup and isolated restore verification passed."
