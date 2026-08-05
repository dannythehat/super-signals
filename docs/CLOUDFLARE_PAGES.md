# Cloudflare frontend deployment

Super Signals uses Cloudflare Workers Static Assets for the React frontend. Cloudflare now recommends Workers Static Assets for new static sites and single-page applications. The Python API and 24-hour Telegram/trading services remain separate.

## Connected build settings

The existing Cloudflare project `super-signals` is connected to `dannythehat/super-signals`.

Use:

- Production branch: `main`
- Root directory: repository root
- Build command: `npm install && npm run build:web`
- Deploy command: `npx wrangler deploy`
- Non-production deploy command: `npx wrangler versions upload`
- Node version: `22`

The root `wrangler.jsonc` identifies the Worker and serves `apps/web/dist` as static assets. SPA fallback routing is configured with `not_found_handling: single-page-application`.

## Environment variables

Build-time browser values:

```text
VITE_API_BASE_URL=/api
VITE_DEPLOYMENT_ENV=production
```

Non-production builds may use `VITE_DEPLOYMENT_ENV=preview` once separate preview build variables are enabled.

Never place Telegram, MT5, database, Cloudflare API or encryption secrets in `VITE_` variables. Vite embeds `VITE_` values into the browser bundle and they are public.

## Acceptance check

Day 3 passes when:

1. Cloudflare completes a successful build from the repository.
2. The generated `workers.dev` URL loads on phone and desktop.
3. React assets and SPA fallback routing work.
4. GitHub CI is green.
5. The successful Day 3 change is merged to `main`.
