#!/bin/sh
set -eu

cd /app/services/api
alembic -c alembic.ini upgrade head
python -m app.retire_tdc_provider_once
python -m app.bootstrap
# Read-only broker connection diagnostics. This never reads the member MT5 password,
# changes the broker account, enables trading, or creates a trade action.
python -m app.mt5_diagnostic_once &
# Owner-authorized one-time recovery for a live member terminal that MetaAPI left
# deployed but disconnected. During the canonical weekly freeze it waits in the
# background until Monday 01:01 Europe/Sofia, then retries without needing the MT5
# password because MetaAPI already holds the provisioned terminal credentials.
python -m app.mt5_redeploy_after_market_open &
# Owner-authorized partner onboarding is fully environment-gated and runs in the
# background so a broker/API delay can never hold the web service health check.
python -m app.partner_onboarding_once &
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
