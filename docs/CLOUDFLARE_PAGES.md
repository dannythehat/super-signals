# Cloudflare frontend deployment

Super Signals uses Cloudflare Workers Static Assets for the React frontend. The Python API and future 24-hour Telegram and trading services remain separate.

## Deployment setup

The repository deploys through GitHub Actions using the encrypted `CLOUDFLARE_API_TOKEN` repository secret.

- Production source: pushes to `main`
- Preview source: pull requests targeting `main`
- Build command: `npm run build:web`
- Static assets directory: `apps/web/dist`
- Preview Worker: `super-signals-preview`
- Production Worker: `super-signals`
- Node version: `22`
- Deployment CLI: locked repository dependency `wrangler`

The root `wrangler.jsonc` serves `apps/web/dist` as static assets. SPA fallback routing is configured with `not_found_handling: single-page-application`.

The shared preview Worker represents the latest reviewed pull request. It contains no live credentials and must never be connected to live trading.

## Environment variables

Preview builds use:

```text
VITE_API_BASE_URL=/api
VITE_DEPLOYMENT_ENV=preview
```

Production builds use:

```text
VITE_API_BASE_URL=/api
VITE_DEPLOYMENT_ENV=production
```

Never place Telegram, MT5, database, Cloudflare API or encryption secrets in `VITE_` variables. Vite embeds `VITE_` values into the browser bundle and they are public.

## Deployment gate

A frontend deployment passes when:

1. locked dependencies install through `npm ci`
2. the production dependency audit reports zero vulnerabilities
3. the production web build succeeds
4. Wrangler deploys the correct Worker for the event
5. the HTTPS Worker URL returns the Super Signals application
6. a push to `main` uses the production Worker and a pull request uses the preview Worker

The hosted FastAPI service will use a separate deployment process and private environment variables when that phase begins.
