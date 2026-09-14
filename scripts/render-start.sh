#!/bin/sh
set -eu

cd /app/services/api
alembic -c alembic.ini upgrade head
python -m app.retire_tdc_provider_once
python -m app.bootstrap

# MT5 member onboarding/reconnect is handled only by Connection V2. Legacy one-off
# diagnostics, deferred redeploy jobs and environment-driven partner onboarding are
# intentionally not started here because they can race the canonical connection flow.

# Provider Intelligence remains broker-isolated. Refresh the frozen Day 13
# conditional research evidence first, then run Day 14 research governance.
# Day 14 may only govern provider_research_profiles through learning -> shadow ->
# qualified. Qualified is paper-research eligibility only; this subsystem cannot
# mutate sources.status or grant live-money authority.
(
  python -m app.provider_day13_runtime &&
  python -m app.provider_day14_governance
) &

# Quality contract marker: uvicorn app.main:app
exec python -m app.serve_runtime
