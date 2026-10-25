# PU Prime Windows bridge worker recovery

These scripts supervise the PU Prime bridge in an existing interactive Windows desktop session.
**They do not guarantee that GUI MT5 starts after a cold reboot with no user logged in.**

## Preconditions
- Broker source: MT5 login 36394935, broker server PUPrime-Live 6, gold XAUUSD.s.
- Existing live Render -> Contabo -> MT5 read-only smoke has passed.
- Run task under the exact same Windows desktop account that has MT5 authenticated.
- Broker login password must never be placed in a scheduled task argument.

## Installation
Install the three scripts from this directory into the existing local bridge windows folder:
- run-safe.ps1: avoids PowerShell 5.1 NativeCommandError on Python logging.
- pu-watchdog.ps1: checks every 20 seconds and starts a missing worker only once.
- install-pu-watchdog.ps1: creates an interactive-at-logon Task Scheduler entry.

PowerShell:
& "$env:LOCALAPPDATA\SuperSignalsBridge\local_mt5_bridge\windows\install-pu-watchdog.ps1"
Get-ScheduledTask -TaskName SuperSignals-PU-Worker-Watchdog

A Windows venv python wrapper and the child interpreter are one worker, not two.

## Still unverified for actual live followers
1. Real Super Signals trade decisions to PU master, target sizing, mapping, and lifecycle.
2. Broker order execution, TP/SL/close, and follower copying during an open market.
3. Reconciliation after interrupted commands and trading account/session changes.
4. Cold reboot with NO interactive Windows desktop user logged in.

Do not promise automated follower trading until the remaining gates are proven.