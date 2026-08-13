FROM python:3.13-slim AS security-scan

WORKDIR /scan
COPY . /scan
RUN python scripts/day39_secret_scan.py /scan && touch /scan/.day39-secret-scan-passed

FROM node:22-bookworm-slim AS web-quality

WORKDIR /build
COPY package.json package-lock.json ./
COPY apps/web/package.json ./apps/web/package.json
COPY packages/shared/package.json ./packages/shared/package.json
RUN npm ci
COPY apps ./apps
COPY packages ./packages
RUN npm run typecheck \
    && npm run lint:web \
    && npm run test:web \
    && VITE_API_BASE_URL= npm run build:web \
    && touch /build/.day40-web-quality-passed

FROM python:3.13-slim AS api-quality

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/quality/services/api

WORKDIR /quality
COPY requirements-dev.txt ./requirements-dev.txt
COPY services/api/requirements.txt ./services/api/requirements.txt
RUN python -m pip install --no-cache-dir -r requirements-dev.txt
COPY pytest.ini ./
COPY services/api ./services/api
RUN python -m compileall -q services/api/app services/api/migrations \
    && python -m pytest services/api/tests \
    && touch /quality/.day40-api-quality-passed

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
COPY --from=web-quality /build/apps/web/dist /app/web-dist
COPY --from=security-scan /scan/.day39-secret-scan-passed /tmp/.day39-secret-scan-passed
COPY --from=web-quality /build/.day40-web-quality-passed /tmp/.day40-web-quality-passed
COPY --from=api-quality /quality/.day40-api-quality-passed /tmp/.day40-api-quality-passed
RUN test -f /tmp/.day39-secret-scan-passed \
    && test -f /tmp/.day40-web-quality-passed \
    && test -f /tmp/.day40-api-quality-passed \
    && rm /tmp/.day39-secret-scan-passed \
          /tmp/.day40-web-quality-passed \
          /tmp/.day40-api-quality-passed \
    && chmod 0755 /app/scripts/render-start.sh

EXPOSE 10000
CMD ["/app/scripts/render-start.sh"]
