# Super Signals local MT5 bridge

This worker replaces only MetaAPI's broker transport. Signal parsing, provider
attribution, per-leg risk sizing, multi-TP/layer allocation, management, settlement,
Telegram publishing, website reporting and PostgreSQL remain in the Render service.

The laptop makes outbound HTTPS requests to Render. No router port-forwarding, public
IP address or inbound firewall rule is needed. Vantage login/password/server values stay
in the laptop's local `.env` and are never sent to Render.

## Isolation from GoldThinker

Use all four controls together:

1. A separate Windows task named `SuperSignalsLocalBridge`.
2. A separate install/data/log folder: `%LOCALAPPDATA%\SuperSignalsBridge`.
3. A dedicated magic number (`2601001` by default) that is not GoldThinker's magic.
4. Super Signals is the sole writer on any MT5 account it manages. If GoldThinker reads
   that same account, GoldThinker must remain observe-only; otherwise use a second MT5
   account/terminal.

The MT5 account must be **hedging**, not netting, because Super Signals intentionally
opens one independent position for each TP/runner leg.

## Render settings (do not enable yet)

Run migration `0126_local_mt5_bridge`, then configure:

```text
SUPER_SIGNALS_LOCAL_BRIDGE_TOKEN=<same random 32+ character secret as laptop>
SUPER_SIGNALS_LOCAL_BRIDGE_PROFILE=super-signals
SUPER_SIGNALS_LOCAL_BRIDGE_COMMAND_TIMEOUT_SECONDS=30
SUPER_SIGNALS_BROKER_TRANSPORT=metaapi
```

Keep `SUPER_SIGNALS_BROKER_TRANSPORT=metaapi` during installation and read-only parity
testing. The eventual cutover value is `local_bridge`.

## Laptop installation

1. Install the Vantage MT5 terminal and sign into the intended account.
2. Install 64-bit Python 3.11. The official `MetaTrader5` package is Windows-only.
3. Copy `.env.example` to a private `.env` and fill every required blank. The account
   reference is the current `metaapi_account_id` stored in Super Signals; it is only a
   routing identifier after cutover, not a MetaAPI credential.
4. From PowerShell in this directory run:

```powershell
powershell -ExecutionPolicy Bypass -File .\windows\install.ps1 -EnvFile .\.env
```

5. Check `%LOCALAPPDATA%\SuperSignalsBridge\logs\bridge.log` and Windows Task Scheduler.

The task starts at Windows logon and restarts after failures. Windows and MT5 must be
running; sleep/hibernation pauses execution. Set the laptop to remain awake while
plugged in before enabling live mutation.

## Safe cutover sequence

1. Deploy schema/API with transport still `metaapi`.
2. Start the worker and confirm heartbeat plus read-only account/quote/position parity.
3. Pause new automatic entries and reconcile all existing positions/orders.
4. Change transport to `local_bridge`, restart Render and test one demo signal.
5. Verify each TP leg's volume/SL/TP, website state and Telegram lifecycle output.
6. Re-enable automatic entries only after the demo acceptance checklist passes.

Do not run both transports as execution writers during cutover.
