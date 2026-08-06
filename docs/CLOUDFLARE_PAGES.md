# Cloudflare frontend deployment

Super Signals uses Cloudflare Workers Static Assets for the React frontend. The Python API and 24-hour Telegram/trading services remain separate.

## Deployment setup

The repository deploys through GitHub Actions using the encrypted `CLOUDFLARE_API_TOKEN` repository secret.

- Production branch: `main`
- Preview branch: `feature/day-03-cloudflare-preview`
- Build command: `npm run build:web`
- Static assets directory: `apps/web/dist`
- Preview Worker: `super-signals-preview`
- Production Worker: `super-signals`
- Node version: `22`

The root `wrangler.jsonc` serves `apps/web/dist` as static assets. SPA fallback routing is configured with `not_found_handling: single-page-application`.

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

## Acceptance check

Day 3 passes when:

1. GitHub CI passes.
2. The preview Worker deploys successfully.
3. The preview URL loads on phone and desktop.
4. The app displays `Preview` on the preview deployment.
5. The merged `main` branch deploys the production Worker displaying `Production`.
