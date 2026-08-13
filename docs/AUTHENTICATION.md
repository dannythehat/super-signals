# Authentication and session security

Super Signals uses one database-backed authentication boundary for Owner, Trading Admin and invited User accounts. Day 39 hardens the original Day 5 foundation without changing role permissions or trading identity.

## Passwords and tokens

- Passwords are stored only as salted scrypt hashes.
- Login creates a random opaque session token.
- Only the SHA-256 hash of a session token is stored in PostgreSQL.
- The browser receives the raw session token only in an `HttpOnly`, `SameSite=Strict` cookie.
- Preview/production cookies are `Secure`.
- Recovery/setup tokens are hashed before storage and are never returned by ordinary read APIs.
- Creating a new ordinary password-recovery request terminally invalidates older unused recovery requests for that user. Historical rows remain for audit.
- Telegram sessions, broker credentials, publisher tokens, encryption keys and other infrastructure secrets remain outside normal application/audit payloads.

## Session lifetime and device binding

- The normal preview session window is explicitly **8 hours** (`28800` seconds).
- Active sessions may slide while the same browser remains in use, but no application session can exceed a **30-day absolute lifetime**, even if an environment value is accidentally configured longer.
- The already-stored keyed-HMAC User-Agent fingerprint is now enforced on authenticated requests. Presenting a stolen cookie from a different browser/User-Agent revokes that individual session and returns unauthenticated.
- IP fingerprints remain privacy-preserving diagnostic evidence and are **not** used as a hard session binding because legitimate mobile networks can rotate addresses.
- Logout and Owner revoke continue to revoke database sessions immediately.

## Persistent authentication throttling

Day 39 adds PostgreSQL-backed throttling so protection survives process restarts and multiple application instances.

- Login account+IP bucket: 5 failures per 15-minute window, then a 15-minute block.
- Login IP bucket: 30 failures per 15-minute window, then a 15-minute block.
- Password recovery: 10 requests per 15-minute window per client IP.
- Trading Admin one-time setup: 10 failed attempts per 15-minute window per client IP.
- Rate-limit keys are keyed-HMAC hashes. Raw email/IP bucket material is not stored in `auth_rate_limits`.
- A blocked request returns HTTP `429` with `Retry-After` and never reaches password/token verification.

These limits are security controls, not trading controls. They cannot place, modify, close or replay broker trades.

## Non-enumeration

- Invalid login returns one generic email/password error.
- Password recovery always returns the same accepted response whether or not an eligible account exists.
- Registration/invitation rejection remains generic to callers while a safe reason is retained in audit evidence.

## Required deployed settings

```text
SUPER_SIGNALS_ENV=preview|production
SUPER_SIGNALS_COOKIE_SECURE=true
SUPER_SIGNALS_SESSION_TTL_SECONDS=28800
SUPER_SIGNALS_FINGERPRINT_SECRET=<high-entropy secret>
SUPER_SIGNALS_CORS_ORIGINS=<exact allowed origins>
DATABASE_URL=<managed PostgreSQL connection>
```

Never place secret values in browser-facing `VITE_` variables, source control, Notion, screenshots or support messages.

## Source secret gate

Every Docker build now runs `scripts/day39_secret_scan.py` before a runtime image can be produced. It scans the source build context for high-confidence credential patterns and reports only file path + rule name, never the matched value. Only exact known fake/local fixtures are allow-listed; whole files or test directories are not exempted.

## Routes

- `POST /auth/login`
- `GET /auth/me`
- `POST /auth/logout`
- `POST /auth/recovery`
- `POST /auth/admin-setup`
- `POST /auth/register` (owner-issued invitation flow)

All protected application routes use the same database session identity and role/permission matrix.
