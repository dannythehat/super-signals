FROM node:22-bookworm-slim AS web-build

WORKDIR /build

# Keep dependency installation in a stable Docker layer. Ordinary source-code
# edits no longer invalidate npm ci unless a package manifest/lockfile changes.
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
RUN python -m pip install --no-cache-dir pytest && cd /app/services/api && pytest -q \
    tests/test_admin_portfolio_day35.py \
    tests/test_admin_user_controls_day35.py \
    tests/test_control_centre_day35.py \
    tests/test_day35_mapped_only_stop_behavior.py \
    tests/test_day35_mt5_onboarding.py \
    tests/test_day35_role_privacy.py \
    tests/test_operations_day35.py \
    tests/test_trade_identity_day35.py \
    tests/test_day29_invitations.py \
    tests/test_authentication.py \
    tests/test_day31_risk_activation_controls.py \
    tests/test_day32_dashboard_contract.py
COPY scripts/render-start.sh /app/scripts/render-start.sh
COPY --from=web-build /build/apps/web/dist /app/web-dist
RUN chmod 0755 /app/scripts/render-start.sh

EXPOSE 10000
CMD ["/app/scripts/render-start.sh"]
