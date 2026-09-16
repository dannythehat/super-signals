#!/bin/sh
set -eu

cd /app/services/api
alembic -c alembic.ini upgrade head
python -m app.retire_tdc_provider_once
python -m app.bootstrap

# MT5 member onboarding/reconnect is handled only by Connection V2. Legacy one-off
# diagnostics, deferred redeploy jobs and environment-driven partner onboarding are
# intentionally not started here because they can race the canonical connection flow.

# Provider Intelligence remains broker-isolated. Day 13 now owns a bounded forward
# refresh loop: it first replays only v1 context misses that the v2 D1-only policy can
# legitimately reconsider, then records at most one conditional evidence run per code
# SHA per UTC day. Day 14 governance remains research-only and cannot grant live money
# authority or mutate live provider routing.
python -m app.provider_day13_runtime --forever &
python -m app.provider_day14_governance &

# Quality contract marker: uvicorn app.main:app
exec python -m app.serve_runtime
