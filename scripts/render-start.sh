#!/bin/sh
set -eu

cd /app/services/api
alembic -c alembic.ini upgrade head
python -m app.retire_tdc_provider_once
python -m app.bootstrap
# Day 11 acceptance is research-only and idempotent by code SHA/tolerance. Run it in
# the background so broker/member routing and the health endpoint are never delayed.
python -m app.provider_day11_acceptance &
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-10000}"
