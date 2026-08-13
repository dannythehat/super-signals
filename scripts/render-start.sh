#!/bin/sh
set -eu

cd /app/services/api
alembic -c alembic.ini upgrade head
python -m app.bootstrap

if [ "${SUPER_SIGNALS_DAY39_SECURITY_PROBE:-0}" = "1" ]; then
  python -c 'from app.day39_security_probe import run_day39_security_probe; run_day39_security_probe()'
fi

if [ "${SUPER_SIGNALS_DAY39_BACKUP_ACCEPTANCE:-0}" = "1" ]; then
  python -c 'from app.day39_backup_acceptance import run_day39_backup_restore_acceptance; run_day39_backup_restore_acceptance()'
fi

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-10000}"
