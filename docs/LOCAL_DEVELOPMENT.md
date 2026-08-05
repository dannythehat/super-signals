# Local Development

## Requirements

- Node.js 20 or newer
- npm 10 or newer
- Python 3.11 or newer

## First setup

```bash
npm install
python -m venv .venv
```

Activate the virtual environment, then install the API dependencies:

```bash
python -m pip install -r requirements-dev.txt
```

## Run the API

```bash
npm run dev:api
```

The API is available at `http://127.0.0.1:8000` and its health endpoint is
`http://127.0.0.1:8000/health`.

## Run the web app

In a second terminal:

```bash
npm run dev:web
```

The web app is available at `http://127.0.0.1:5173`. Vite proxies `/api` requests to the local FastAPI service.

## Run all checks

```bash
npm run check
```

This runs TypeScript checking, JavaScript and Python linting, formatting checks, frontend and backend tests, and a production frontend build.

## Credentials

No real credentials are required for the foundation scaffold. Never add Telegram, MT5, database or Cloudflare secrets to tracked files.
