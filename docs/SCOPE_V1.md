# Version 1 Scope

This document defines the first invite-only version of Super Signals. Anything outside this boundary requires an explicit scope decision before implementation.

## In scope

### Access and roles

- Owner Admin with full platform control
- Trading Admin with source and trading-operations permissions
- Invite-only users registered with an approved email and one-time access key
- One approved Vantage MT5 account per user
- Passkey/biometric-capable login flow with secure fallback

### Telegram sources

- Multiple authorised Telegram reader accounts per administrator
- At least four connected reader accounts for the Owner Admin and four for the Trading Admin
- Groups and channels shown for explicit source selection
- Personal one-to-one chats excluded from the admin interface and message pipeline
- Testing, Live and Paused source states
- Source access-loss detection
- Provider permission recorded before a source becomes Live

### Message handling

- New trade, trade update and chatter classification
- Provider-specific deterministic parsers
- Unknown or unclear wording skipped and sent to admin review
- Duplicate protection across accounts and providers
- Edited-message processing
- Deleted source messages cause no trade action
- Explicit follow-ups linked to the correct open signal

### Super Signals channel

- Private branded Telegram channel
- Clean reposting rather than forwarding
- No original provider identity, username, link or Forwarded From label
- Trade updates linked to the original Super Signals post
- Verified app users may receive channel access
- Telegram publication remains separate from the internal execution source of truth

### Trading

- Vantage MT5 initially
- XAUUSD BUY and SELL signals
- One separate position per TP level
- Shared posted entry and SL, individual TP values
- 0.5% or 1% risk per position
- Explicit double-size instruction doubles the selected per-position risk
- One-time entry-price check; unavailable entry is skipped
- Complete-signal funds/margin check; no partial execution
- Close one, close all, move SL to entry, change SL/TP and cancel pending order
- Manual MT5 changes are respected, recorded and never reversed automatically
- User stopping or owner revocation closes bot-managed positions only
- Reconnect and reconciliation without replaying missed signals

### User experience

- Mobile-first PWA
- User, Owner Admin and Trading Admin interfaces
- Trading activation and stopping controls
- Balance, open P/L, completed profit and position status
- Signal timeline and cash/percentage performance
- In-app and push notifications
- Source identity hidden from normal users

### Audit and testing

- Append-only supporting audit trail
- Seven-year retention design
- Separate Telegram test group and Owner demo MT5 account
- Strict separation between Test and Live
- Controlled Owner-only live pilot before any external user trades
- Europe-focused legal/compliance review as a live-launch gate

## Explicitly out of scope for version 1

- Public registration or an open consumer launch
- User-selected providers or groups
- Multiple broker support
- Multiple MT5 accounts per user
- Native iOS or Android applications
- Subscription billing or public pricing
- Social chat, comments or general community messaging inside the app
- Reposting ordinary Telegram conversation
- Reading or displaying administrators' personal Telegram chats
- Open-ended AI deciding whether an unclear message should trade
- Chasing an unavailable market entry or silently substituting a different price
- Partial signals when the full instructed trade cannot be funded
- Automatically reopening a position changed or closed manually
- Copying the Owner's own master trade as the execution trigger
- Public performance marketing before verified live evidence and legal approval

## Change control

A requested feature outside this document must be recorded as a new decision, assessed for security and trading impact, and scheduled rather than silently added during the current build day.
