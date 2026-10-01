#!/bin/sh
set -eu

cd /app/services/api
alembic -c alembic.ini upgrade head
python -m app.retire_tdc_provider_once
python -m app.bootstrap

# MT5 member onboarding/reconnect is handled only by Connection V2. Legacy one-off
# diagnostics, deferred redeploy jobs and environment-driven partner onboarding are
# intentionally not started here because they can race the canonical connection flow.

# Provider Intelligence remains broker-isolated. The learning loop refreshes whenever
# usable forward PIT/OOS evidence changes (checked every 15 minutes by default) and
# replays only genuine v1 Provider Context misses once under the D1-only v2 contract.
# Governance v2 evaluates each provider from its own immutable preregistration boundary.
# Neither process can grant live-money authority or mutate live provider routing.
python -m app.provider_day13_runtime --forever &
python -m app.provider_governance_runtime_v2 &

# Quality contract marker: uvicorn app.main:app
exec python -m app.serve_runtime
