#!/bin/sh
set -eu

cd /app/services/api
alembic -c alembic.ini upgrade head
python -m app.bootstrap

if [ "${SUPER_SIGNALS_DAY28_CODE_ACCEPTANCE:-0}" = "1" ]; then
  python -m app.day28_code_acceptance
fi

if [ "${SUPER_SIGNALS_DAY28_LIVE_ACCEPTANCE:-0}" = "1" ]; then
  python -c 'import asyncio; from app.db import get_session_factory; from app.day28_live_acceptance import run_day28_live_acceptance; asyncio.run(run_day28_live_acceptance(session_factory=get_session_factory()))' &
fi

if [ "${SUPER_SIGNALS_DAY28_ACCEPTANCE_RECOVERY:-0}" = "1" ]; then
  python -c 'import asyncio; from app.db import get_session_factory; from app.day28_acceptance_recovery import run_day28_acceptance_recovery; asyncio.run(run_day28_acceptance_recovery(session_factory=get_session_factory()))' &
fi

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-10000}"
