#!/bin/sh
set -eu

cd /app/services/api
alembic -c alembic.ini upgrade head
python -m app.bootstrap

if [ "${SUPER_SIGNALS_DAY40_DATABASE_ACCEPTANCE:-0}" = "1" ]; then
  python -c 'from app.day40_database_acceptance import run_day40_database_acceptance; run_day40_database_acceptance()'
fi

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-10000}"
