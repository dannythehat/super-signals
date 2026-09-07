#!/bin/sh
set -eu

cd /app/services/api
alembic -c alembic.ini upgrade head
python -m app.retire_tdc_provider_once
python -m app.bootstrap
# Provider Intelligence remains broker-isolated. Refresh the frozen Day 13
# conditional research evidence first, then run Day 14 research governance.
# Day 14 may only govern provider_research_profiles through learning -> shadow ->
# qualified. Qualified is paper-research eligibility only; this subsystem cannot
# mutate sources.status or grant live-money authority.
(
  python -m app.provider_day13_runtime &&
  python -m app.provider_day14_governance
) &
# Day 15 is a separate fail-safe monthly audit sidecar. It waits for a completed
# Day 14 governance run from this exact deploy SHA before persisting candidates.
# It cannot mutate provider state or acquire broker/live-money authority.
python -m app.provider_day15_monthly_audit &
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-10000}"
