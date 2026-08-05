# Cloudflare Pages deployment

Super Signals uses Cloudflare Pages for the React frontend. The Python API and 24-hour Telegram/trading services are deployed separately.

## Git integration settings

Create a Cloudflare Pages project connected to `dannythehat/super-signals` with:

- Production branch: `main`
- Root directory: repository root
- Build command: `npm ci && npm run build:web`
- Build output directory: `apps/web/dist`
- Node version: `22`

Pull requests and non-production branches should create preview deployments. Commits merged into `main` should update the production Pages deployment.

## Environment separation

Set these variables independently in Cloudflare for Preview and Production:

### Preview

```text
VITE_DEPLOYMENT_ENV=preview
VITE_API_BASE_URL=/api
```

### Production

```text
VITE_DEPLOYMENT_ENV=production
VITE_API_BASE_URL=/api
```

The values may initially point to the same placeholder route, but they must remain separate Cloudflare environment entries so the API targets can diverge safely later.

Never place Telegram, MT5, database, Cloudflare API or encryption secrets in `VITE_` variables. Vite embeds `VITE_` values into the browser bundle and they are public.

## Acceptance check

Day 3 passes when:

1. The branch preview URL loads on phone and desktop.
2. The screen shows `Preview` as the environment.
3. A production URL exists for `main` and shows `Production`.
4. Preview and production variables are stored separately.
5. GitHub CI is green before the branch is merged.
