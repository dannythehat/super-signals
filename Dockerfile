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

FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/services/api \
    SUPER_SIGNALS_WEB_DIST=/app/web-dist

WORKDIR /app
COPY services/api/requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/requirements.txt

COPY services/api /app/services/api
COPY requirements-dev.txt /app/requirements-dev.txt
COPY scripts/render-start.sh /app/scripts/render-start.sh
COPY --from=web-build /build/apps/web/dist /app/web-dist

# GitHub Actions are permanently disabled for Super Signals. Validate the exact
# source tree that will run, but install test-only dependencies into an ephemeral
# venv so they cannot alter the runtime Python environment or final process.
RUN python -m compileall -q /app/services/api/app && \
    python -m venv /tmp/day34-validation && \
    /tmp/day34-validation/bin/python -m pip install --no-cache-dir -r /app/requirements-dev.txt && \
    PYTHONPATH=/app/services/api /tmp/day34-validation/bin/python -m pytest -q \
      /app/services/api/tests/test_ai_active_trade_watch_day34.py \
      /app/services/api/tests/test_day34_notification_contract.py \
      /app/services/api/tests/test_provider_pips_day34.py && \
    rm -rf /tmp/day34-validation /app/requirements-dev.txt && \
    chmod 0755 /app/scripts/render-start.sh

EXPOSE 10000
CMD ["/app/scripts/render-start.sh"]
