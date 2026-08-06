# Environment map

Super Signals uses strict environment separation. A deployment must fail rather than silently fall back to development secrets.

| Environment | Frontend | API | Database | Secrets | Trading state |
| --- | --- | --- | --- | --- | --- |
| Local | Vite on `localhost:5173` | FastAPI on `127.0.0.1:8000` | Docker PostgreSQL 16 | Local `.env`, never committed | Disabled |
| CI | Built and tested only | TestClient and migration commands | Disposable PostgreSQL 16 service | Test-only values in workflow | Disabled |
| Preview | `super-signals-preview.dannythehat2.workers.dev` | Not hosted yet | Supabase project `super-signals-preview` | Encrypted deployment secrets | Disabled |
| Production | `super-signals.dannythehat2.workers.dev` | Not hosted yet | Separate production database required before launch | Separate encrypted production secrets | Disabled |

## Required API configuration

Development and test may use local defaults. Every other environment must explicitly provide:

- `SUPER_SIGNALS_ENV`
- `DATABASE_URL`
- `SUPER_SIGNALS_CORS_ORIGINS`
- `SUPER_SIGNALS_FINGERPRINT_SECRET` with at least 32 characters

Optional settings with secure defaults:

- `SUPER_SIGNALS_SESSION_COOKIE`
- `SUPER_SIGNALS_SESSION_TTL_SECONDS`
- `SUPER_SIGNALS_RECOVERY_TTL_SECONDS`
- `SUPER_SIGNALS_COOKIE_SECURE`

## Public frontend configuration

Only non-secret values may be exposed to Vite:

- `VITE_API_BASE_URL`
- `VITE_DEPLOYMENT_ENV`

Telegram sessions, broker credentials, database URLs, Cloudflare tokens and encryption keys must never be prefixed with `VITE_`.

## Promotion path

1. A feature branch passes CI.
2. Its pull request deploys to the shared preview Worker.
3. The preview shell is verified over HTTPS.
4. The pull request is reviewed and merged.
5. `main` deploys the production frontend.
6. Database migrations and API deployment use separate controlled release steps once the API is hosted.

The shared preview Worker is for the latest reviewed pull request. It contains no live credentials and must never be connected to live trading.
