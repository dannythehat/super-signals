#!/bin/sh
set -eu

cd /app/services/api
alembic -c alembic.ini upgrade head
python -m app.retire_tdc_provider_once
python -m app.bootstrap
# Provider Intelligence remains broker-isolated. Refresh the frozen Day 13
# conditional research evidence first, then run Day 14 research governance.
# Day 14 may only move its separate research stage as far as tiny_live_candidate;
# it cannot mutate sources.status or grant live-money authority.
(
  python -m app.provider_day13_runtime &&
  python -m app.provider_day14_governance
) &
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-10000}"
