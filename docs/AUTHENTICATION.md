# Owner authentication

Day 5 adds the first owner-only authentication boundary.

## Security model

- Owner passwords are stored as salted scrypt hashes.
- Login creates a random opaque session token.
- Only the SHA-256 hash of the session token is stored in PostgreSQL.
- The browser receives the raw token only in an `HttpOnly`, `SameSite=Strict` cookie.
- Production cookies are `Secure` by default.
- Sessions expire after eight hours by default and can be revoked immediately.
- Logout revokes the database session before clearing the browser cookie.
- Recovery always returns the same response whether or not an account exists.
- Recovery tokens are hashed before storage and are never returned by the API.
- User-agent and IP fingerprints use keyed HMAC values rather than raw identifiers.
- Passkey and two-factor states are exposed only as setup placeholders in Day 5.

## Required production settings

```text
SUPER_SIGNALS_ENV=production
SUPER_SIGNALS_COOKIE_SECURE=true
SUPER_SIGNALS_FINGERPRINT_SECRET=<high-entropy secret>
SUPER_SIGNALS_CORS_ORIGINS=<exact production and preview origins>
DATABASE_URL=<managed PostgreSQL connection>
```

Never place these values in browser-facing `VITE_` variables.

## Set the owner password

Run this only in a trusted server or administrator environment:

```bash
python -m app.set_owner_password --email owner@example.com
```

The command prompts without echoing the password, hashes it before storage and revokes existing sessions.

## Routes

- `POST /auth/login`
- `GET /auth/me`
- `POST /auth/logout`
- `POST /auth/recovery`

The recovery route establishes the database and privacy boundary. Email delivery and token consumption are deliberately deferred until the notification service exists.
