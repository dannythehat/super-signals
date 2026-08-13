FROM python:3.13-slim AS security-scan

WORKDIR /scan
COPY . /scan
RUN python scripts/day39_secret_scan.py /scan && touch /scan/.day39-secret-scan-passed

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
COPY scripts/render-start.sh /app/scripts/render-start.sh
COPY --from=web-build /build/apps/web/dist /app/web-dist
COPY --from=security-scan /scan/.day39-secret-scan-passed /tmp/.day39-secret-scan-passed
RUN test -f /tmp/.day39-secret-scan-passed \
    && rm /tmp/.day39-secret-scan-passed \
    && chmod 0755 /app/scripts/render-start.sh

EXPOSE 10000
CMD ["/app/scripts/render-start.sh"]
