#!/bin/sh
set -eu

cd /app/services/api
alembic -c alembic.ini upgrade head
python -m app.retire_tdc_provider_once
python -m app.bootstrap
# Day 13 dormant Provider Intelligence harness. It preregisters shadow-provider
# conditional hypotheses before OOS evidence, remains research-only, and cannot
# grant statistical, provider-routing, sizing or live-money authority. The runtime
# wrapper binds the canonical aidy_context_as_of_utc production column.
python -m app.provider_day13_runtime &
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-10000}"
