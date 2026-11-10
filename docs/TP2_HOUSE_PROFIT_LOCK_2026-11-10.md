# TP2 Profit Lock (Super Signals house rule) — 10 November 2026

## Owner mandate

After the broker has confirmed a TP2 profit-taking closure, move the stop-loss on each surviving TP3, TP4, TP5 and runner to its **own broker entry price**. Never loosen existing tighter stops. This rule takes priority over subsequent provider instructions; neither mid-trade provider edits nor stale source stops may undo it. No changes to trade volume, TP, risk caps, existing unrelated positions, or entry eligibility.

## Production location

The only authorised execution path is the existing Contabo PU Prime demo master 700207861 and the existing unified copier to the configured followers FriendDemo, Gina and EllieCent. Render is the Telegram provider intake/feed, not the broker executor. Do not use retired remote vendor/broker gateway logic.

## Source and tests

- \`scripts/tp2_house_lock.py\` is the new independently testable broker-verified TP2 decision engine.
- \`scripts/test_tp2_house_lock.py\` covers TP2 broker-close evidence, partial-close rejection, original position identity, independent follower entry, stop tightening, broker modification readback, and refusal to modify unrelated tickets.
- On Contabo, at \`C:\ProgramData\GoldThinker\SuperSignals\Master700207861-Copier\TP2HouseStage-20261110\`, staged code and integration candidate scripts passed **six pure tests and compilation** on 10 November 2026.
- The master integration runs the house checker on an already connected master MT5 session **before** the provider feed poll, so feed failures cannot suppress protection.
- The follower integration checks the authoritative master TP2 receipt or follower's own confirmed TP2 receipt, using its existing per-follower journal to map only remaining TP3+ legs and each follower's actual MT5 entry.
- Follower \`update_target\` must preserve the tightest existing protective stop to ensure later copier cycles cannot undo the house lock.
- Successful stop modifications must be read back from the broker, and failures recorded for retry rather than silently treated as successful.

## Release status — NOT LIVE

The audited current live master and follower files remain unchanged, and neither running process has loaded the new engine. The new module exists only in the repository and the isolated Contabo staging directory, **not** the production runtime root. On 10 November, a direct remote activation attempt was blocked by safety checks. It was **not** executed, and the running master/copier were not stopped. Read-only re-verification at 07:16 UTC showed exactly one master and one copier still running.

## Required controlled release

Use the established approved production change mechanism. Back up the current master and \`safer_follower_v2.py\`, verify live MT5 master account/positions and legitimate caller identity, deploy the tested module and staged integration scripts, reload the two existing workers with rollback on failure, and verify precisely one healthy process per worker. Verify broker-account identity, follower account identities, feed continuity and log errors. Do not generate synthetic trades. The first natural 3+-TP setup reaching broker-confirmed TP2 will be the full broker and follower end-to-end acceptance, including its confirmation records and alerts. Until it happens, do not claim real broker SLTP protection proven.
