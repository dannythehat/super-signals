#!/bin/sh
set -eu

cd /app/services/api
alembic -c alembic.ini upgrade head
python -m app.owner_email_alignment
python -m app.bootstrap
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-10000}"
