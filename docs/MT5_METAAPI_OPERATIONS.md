# MT5 / MetaAPI operations runbook

This is the permanent operating guide for the Super Signals Vantage MT5 connection.

## Normal user experience

A Super Signals user should **never need a MetaAPI token or encryption key**.

For an MT5 connection, the user provides only:

1. Vantage MT5 login/account number
2. Vantage MT5 trading password
3. exact Vantage MT5 server name

The trading password is used only for the broker connection request and is not stored by Super Signals.

After the first successful MetaAPI setup, Super Signals stores the MetaAPI platform credential encrypted in PostgreSQL. Reconnects and later account onboarding can reuse that encrypted credential automatically.

## Permanent Render configuration

These values are infrastructure, not temporary Day 22 bootstrap values.

### Never clear

- `SUPER_SIGNALS_BROKER_CREDENTIAL_KEYS`
  - primary Fernet/MultiFernet key list used to encrypt/decrypt stored MetaAPI credentials
  - losing every valid key makes existing encrypted credentials unreadable
- `SUPER_SIGNALS_MT5_ENCRYPTION_KEYS`
  - fallback copy used if the primary variable is accidentally removed
  - keep synchronized with the active decrypt key set
- `SUPER_SIGNALS_MT5_RECONCILE_SECONDS`
  - current pre-execution default: `3600`
  - values below 300 seconds are clamped to 300 to protect MetaAPI spend

### Bootstrap / emergency only

Keep these disabled or empty in normal operation:

- `SUPER_SIGNALS_DAY22_BOOTSTRAP_ENABLED=0`
- `SUPER_SIGNALS_DAY22_REKEY_EXISTING_TOKEN=0`
- `SUPER_SIGNALS_DAY22_DIAGNOSTIC_PROBE=0`
- `SUPER_SIGNALS_DAY22_OWNER_ID=`
- `SUPER_SIGNALS_DAY22_DEMO_LOGIN=`
- `SUPER_SIGNALS_DAY22_DEMO_SERVER=`
- `SUPER_SIGNALS_DAY22_DEMO_PASSWORD=`

`SUPER_SIGNALS_API` is optional after at least one MT5 record contains a valid encrypted MetaAPI token. A fresh environment may use it once to bootstrap the first connection, but normal reconnects should resolve the encrypted stored credential instead.

## What is stored

`mt5_accounts` stores:

- Super Signals owner/user ID
- broker = Vantage
- platform = MT5
- demo/live environment flag
- MT5 login
- MT5 server
- MetaAPI account ID
- encrypted MetaAPI token ciphertext
- MetaAPI token fingerprint
- connection state and timestamps

It does **not** store the Vantage MT5 trading password.

The public connection view masks the MT5 login and never returns MetaAPI token ciphertext, plaintext token, encryption keys, or broker passwords.

## Reconnect behavior

On backend startup Super Signals performs one immediate reconciliation for each active MT5 account.

After startup, idle reconciliation uses `SUPER_SIGNALS_MT5_RECONCILE_SECONDS`. During the pre-execution build this is one hour to minimize MetaAPI usage. The owner can also explicitly press **Refresh connection**.

If the account is already `DEPLOYED + CONNECTED`, reconciliation only reads state and records the result. It does not create a second MetaAPI account and does not place a trade.

If the account is disconnected or in error, the owner screen keeps the existing account record and offers:

- **Refresh connection** first
- **Reconnect account** only when Vantage credentials genuinely need to be re-entered

Do not repeatedly reconnect when a simple MetaAPI outage or balance issue is reported.

## Troubleshooting

| Error | Meaning | Action |
| --- | --- | --- |
| `metaapi_permission_denied` | MetaAPI rejected the request, commonly account balance/subscription/permission | Check MetaAPI balance/subscription first, then press Refresh once |
| `metaapi_e_auth` | Vantage MT5 authentication failed | Re-enter MT5 login, MT5 trading password and exact server |
| `broker_credential_decryption_failed` | Stored MetaAPI token cannot be opened with current encryption keys | **Do not create another MetaAPI account.** Restore the permanent encryption-key configuration or use the controlled rekey procedure |
| `metaapi_timeout` | MetaAPI did not answer in time | Wait and refresh once; do not loop reconnects |
| `metaapi_unreachable` | MetaAPI network/service unavailable | Leave the saved account intact and retry later |
| `mt5_account_already_bound` | User is already bound to another login/server | Treat as an account-change workflow, not a reconnect |

## Encryption-key rotation

`MetaApiTokenCipher` uses MultiFernet semantics.

For a safe rotation:

1. generate a new Fernet key outside source control
2. set the primary key list to `NEW_KEY,OLD_KEY`
3. deploy and verify existing MT5 records decrypt and reconcile
4. rotate/re-encrypt stored ciphertext under the new first key
5. verify a backend restart reconnects successfully
6. only then remove the old key from both permanent key variables

Never replace the only valid key before stored ciphertext has been rotated.

## MetaAPI cost controls

- no 15-second idle polling
- default idle check = hourly during the pre-execution build
- hard minimum configurable interval = 5 minutes
- startup performs one connection-state reconciliation
- manual refresh is user initiated
- connection recovery must reuse an existing MetaAPI account when one exists
- never use repeated provisioning attempts as a diagnostic technique

When Day 23+ introduces price/account-state reads or streaming connections, cost behavior must be reviewed deliberately rather than silently increasing polling frequency.

## Day 22 known-good evidence

On 2026-08-11 the owner Vantage MT5 demo account was proven:

- MetaAPI remote state `DEPLOYED`
- MetaAPI connection status `CONNECTED`
- Super Signals local status `connected`
- encrypted token decrypt/fingerprint round trip successful
- clean backend restart reconciled `connected -> connected`
- no Vantage password stored
- no trade action created

PR #34 merged this accepted connection implementation to `main`.