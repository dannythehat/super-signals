# Environment map

Super Signals uses strict environment separation. A deployment must fail rather than silently fall back to development secrets.

| Environment | Frontend | API | Database | Secrets | Trading state |
| --- | --- | --- | --- | --- | --- |
| Local | Vite on `localhost:5173` | FastAPI on `127.0.0.1:8000` | Docker PostgreSQL 16 | Local `.env`, never committed | Disabled |
| CI | Built and tested only | TestClient and migration commands | Disposable PostgreSQL 16 service | Test-only values in workflow | Disabled |
| Preview | `super-signals-preview.dannythehat2.workers.dev` | Shared Render preview API | Render PostgreSQL | Encrypted deployment secrets | Disabled |
| Production | `super-signals.dannythehat2.workers.dev` | Separate production API required before launch | Separate production database required before launch | Separate encrypted production secrets | Disabled until launch gate |

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

## Permanent MT5 / MetaAPI configuration

Once MT5 integration exists, the broker encryption keys are permanent infrastructure secrets:

- `SUPER_SIGNALS_BROKER_CREDENTIAL_KEYS` — primary MultiFernet decrypt/encrypt key set
- `SUPER_SIGNALS_MT5_ENCRYPTION_KEYS` — fallback copy of the active decrypt key set
- `SUPER_SIGNALS_MT5_RECONCILE_SECONDS` — idle state-check interval; preview currently uses `3600`

Do **not** clear the broker encryption keys during temporary credential cleanup. Existing encrypted MetaAPI credentials depend on them.

`SUPER_SIGNALS_API` is a bootstrap/optional MetaAPI platform token. After at least one MT5 account is stored, the application can resolve the encrypted stored MetaAPI token for reconnects and later onboarding, so users do not need to know or supply MetaAPI credentials.

Day 22 bootstrap/recovery flags must remain disabled in normal operation. See `docs/MT5_METAAPI_OPERATIONS.md` for the complete runbook and safe key-rotation procedure.

## Public frontend configuration

Only non-secret values may be exposed to Vite:

- `VITE_API_BASE_URL`
- `VITE_DEPLOYMENT_ENV`

Telegram sessions, broker credentials, database URLs, Cloudflare tokens and encryption keys must never be prefixed with `VITE_`.

## Promotion path

1. A feature branch passes local/controlled validation.
2. The exact candidate commit is deployed to the shared preview service deliberately.
3. The preview shell and required live acceptance are verified over HTTPS.
4. The pull request is reviewed and merged.
5. The stable preview pointer is moved only to an accepted commit.
6. Production promotion remains a separate deliberate release step.

GitHub Actions remain manual-only unless the project blueprint is explicitly changed.

The shared preview environment may contain controlled demo credentials required for acceptance, but live trading stays disabled until the later legal, security and live-trading gates pass.