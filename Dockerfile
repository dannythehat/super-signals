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
RUN npm run typecheck
RUN VITE_API_BASE_URL= npm run build:web

# GitHub Actions are permanently disabled for Super Signals. Run the critical
# Day 34 code contracts inside the deliberate Render build instead. This stage
# is not copied into the runtime image and never receives broker credentials.
FROM python:3.13-slim AS api-validation

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/validate/services/api

WORKDIR /validate
COPY requirements-dev.txt ./requirements-dev.txt
COPY services/api/requirements.txt ./services/api/requirements.txt
RUN python -m pip install --no-cache-dir -r requirements-dev.txt

COPY services/api ./services/api
RUN python -m compileall -q services/api/app
RUN python -m pytest -q \
    services/api/tests/test_ai_active_trade_watch_day34.py \
    services/api/tests/test_day34_notification_contract.py \
    services/api/tests/test_provider_pips_day34.py

FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/services/api \
    SUPER_SIGNALS_WEB_DIST=/app/web-dist

WORKDIR /app
COPY services/api/requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/requirements.txt

COPY services/api /app/services/api
COPY scripts/render-start.sh /app/scripts/render-start.sh
COPY --from=web-build /build/apps/web/dist /app/web-dist
COPY --from=api-validation /validate/services/api/app /tmp/day34-validated-app
RUN rm -rf /tmp/day34-validated-app && chmod 0755 /app/scripts/render-start.sh

EXPOSE 10000
CMD ["/app/scripts/render-start.sh"]
