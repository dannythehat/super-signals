# Local Development

## Requirements

- Node.js 20 or newer
- npm 10 or newer
- Python 3.11 or newer
- Docker with PostgreSQL 16 for database-backed tests

## First setup

Copy the non-secret example configuration and install the locked dependencies:

```bash
cp .env.example .env
npm ci
python -m venv .venv
```

Activate the virtual environment, then install the API dependencies:

```bash
python -m pip install -r requirements-dev.txt
```

Start the local database and apply the migrations:

```bash
docker compose up -d postgres
python -m alembic -c services/api/alembic.ini upgrade head
```

## Run the API

```bash
npm run dev:api
```

The API is available at `http://127.0.0.1:8000` and its health endpoint is `http://127.0.0.1:8000/health`.

## Run the web app

In a second terminal:

```bash
npm run dev:web
```

The web app is available at `http://127.0.0.1:5173`. Vite proxies `/api` requests to the local FastAPI service.

## Run all checks

```bash
npm audit --audit-level=low
npm run check
```

This runs dependency auditing, TypeScript checking, JavaScript and Python linting, formatting checks, frontend and backend tests, and a production frontend build. CI also audits Python packages and performs an isolated database backup-and-restore test.

## Dependency changes

Update `package.json` deliberately, regenerate `package-lock.json`, run the complete audit and test suite, and commit both files together. Do not use `npm audit fix --force` without reviewing the proposed breaking changes.

## Credentials

No real credentials are required for the foundation scaffold. Never add Telegram, MT5, database or Cloudflare secrets to tracked files. Non-development API environments refuse to start without explicit database, CORS and session-fingerprint configuration.
