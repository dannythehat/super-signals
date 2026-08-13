#!/bin/sh
set -eu

cd /app/services/api
alembic -c alembic.ini upgrade head
python -m app.bootstrap
python -m app.acceptance_invite
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-10000}"
