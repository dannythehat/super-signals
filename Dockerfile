FROM node:22-bookworm-slim AS web-build

WORKDIR /build
COPY package.json package-lock.json ./
COPY apps/web/package.json ./apps/web/package.json
COPY packages/shared/package.json ./packages/shared/package.json
RUN npm ci
COPY apps ./apps
COPY packages ./packages
RUN VITE_API_BASE_URL= npm run build:web

FROM python:3.13-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/services/api \
    SUPER_SIGNALS_WEB_DIST=/app/web-dist
WORKDIR /app
COPY services/api/requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/requirements.txt
COPY services/api /app/services/api
COPY --from=web-build /build/apps/web/dist /app/web-dist
RUN python -m pip install --no-cache-dir pytest pytest-asyncio && cd /app/services/api && pytest -q \
    tests/test_day38_multi_user_distribution.py \
    tests/test_day37_reconnect_restart_safety.py \
    tests/test_day36_manual_mt5_reconciliation.py \
    tests/test_day36_provider_update_remaining_positions.py \
    tests/test_day28_full_execution.py \
    tests/test_day26_multi_tp_execution.py \
    tests/test_day35_mapped_only_stop_behavior.py \
    tests/test_day35_role_privacy.py \
    tests/test_day31_risk_activation_controls.py \
    tests/test_day32_dashboard_contract.py
COPY scripts/render-start.sh /app/scripts/render-start.sh
RUN chmod 0755 /app/scripts/render-start.sh
EXPOSE 10000
CMD ["/app/scripts/render-start.sh"]
