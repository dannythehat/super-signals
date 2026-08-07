FROM node:22-bookworm-slim AS web-build

WORKDIR /build
COPY package.json package-lock.json ./
COPY apps ./apps
COPY packages ./packages
RUN npm ci
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
RUN chmod 0755 /app/scripts/render-start.sh

EXPOSE 10000
CMD ["/app/scripts/render-start.sh"]
