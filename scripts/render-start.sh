#!/bin/sh
set -eu

cd /app/services/api
alembic -c alembic.ini upgrade head
python -m app.retire_tdc_provider_once
python -m app.bootstrap
# Day 12 dormant Provider Intelligence harness. It reads only forward/PIT score-eligible
# paper evidence, remains research-only, and cannot grant statistical or live-money authority.
python -m app.provider_day12_fingerprint &
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-10000}"
