#!/bin/sh
set -eu

cd /app/services/api
alembic -c alembic.ini upgrade head
python -m app.retire_tdc_provider_once
python -m app.bootstrap
# Day 11 fidelity closure: run the corrected calibration replay over the frozen
# 82-trade corpus. This stays research-only and runs in the background so broker/member
# routing and the health endpoint are never delayed.
python -m app.provider_day11_widened_diagnostic &
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-10000}"
