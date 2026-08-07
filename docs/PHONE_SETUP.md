# Day 8 phone setup

This setup is intentionally limited to a temporary test deployment. It does not enable message listening, parsing, MT5 execution or live trading.

## What the deployment creates automatically

- one full-stack Super Signals web service
- one private PostgreSQL database
- database migrations
- the initial Owner account
- strong fingerprint and Telegram-session encryption secrets
- a secure same-origin website and API

You do not need to configure Cloudflare, Railway, Supabase, CORS, database URLs, migration commands or encryption keys.

## Before opening the deployment link

Create a Telegram application at [my.telegram.org/apps](https://my.telegram.org/apps). Use a dedicated Telegram test account where possible.

Keep these two values ready:

- `api_id`
- `api_hash`

Never paste either value into GitHub, Notion, chat, screenshots or frontend variables.

Choose an Owner password containing 12 to 128 characters. Store it in your password manager.

## One-click deployment

Open the [Super Signals Day 8 deployment](https://render.com/deploy?repo=https%3A%2F%2Fgithub.com%2Fdannythehat%2Fsuper-signals%2Ftree%2Ffeature%2Fday-08-secure-telegram-connection).

If Render asks for GitHub access, connect the GitHub account that can access the private `dannythehat/super-signals` repository.

Render will show four blank private fields:

1. `SUPER_SIGNALS_OWNER_EMAIL` — your sign-in email
2. `SUPER_SIGNALS_OWNER_PASSWORD` — your new 12+ character password
3. `TELEGRAM_API_ID` — the numeric Telegram application ID
4. `TELEGRAM_API_HASH` — the 32-character Telegram application hash

Enter those values and approve the Blueprint. Leave all generated values unchanged.

## Connect Telegram from one phone

When Render shows the web service as Live:

1. Open its `onrender.com` address.
2. Sign in with the Owner email and password entered during deployment.
3. Open **Manage Telegram accounts**.
4. Enter a private label such as `Day 8 test reader`.
5. Enter the phone number linked to Telegram in international format, including `+` and country code.
6. Tap **Send Telegram code**.
7. Switch to Telegram and read the one-time login code Telegram sends to the account.
8. Return to Super Signals and enter that code.
9. Enter the Telegram two-step-verification password only if Telegram requests it.

The login code and two-step password are never stored or written to the audit log. The phone number is masked in the UI and audit evidence. The approved account record retains the Telegram phone number for identity verification, while the portable Telegram session itself is stored encrypted for restart-safe access.

The app should show the account as connected with a masked phone number.

### Optional QR method

QR login remains available for users who have a second already-authorised device. It is no longer the default mobile flow.

## Restart acceptance test

In Render, open the Super Signals web service and choose **Manual Deploy → Restart service**. When the service is Live again, reopen the app and tap **Verify**.

The expected message is:

> The encrypted Telegram session survived restart and is still authorised.

## Disconnect acceptance test

In Super Signals, tap **Disconnect**, then **Confirm disconnect**.

The account must become disconnected and verification must stop working. In Telegram, check **Settings → Devices** and confirm the linked session has disappeared.

## Safe evidence

Safe screenshots may show the masked connected status, successful Verify result and disconnected status. Do not capture login codes, QR codes, full phone numbers, Telegram API hash, passwords, database URLs or generated secrets.
