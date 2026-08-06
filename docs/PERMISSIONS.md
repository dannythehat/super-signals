# Roles and permissions

Super Signals uses database-backed role permissions. Every signed-in account receives only the permissions assigned to its role. API routes enforce the same matrix used by the interface.

## Owner Admin

The Owner Admin can access every permission, including users, access keys, administrator roles, MT5 approvals, security, environments, Telegram sources, trading review, emergency controls and user trading areas.

## Trading Admin

The Trading Admin can manage Telegram sources, move them between Testing and Live, view original source messages, review parsing and trade execution, view activity and use the emergency trading stop.

The Trading Admin cannot manage users, access keys, administrators, MT5 approvals, protected security settings or platform deletion.

## Invited User

An invited user can connect one approved MT5 account, manage the allowed risk setting, activate or stop automated trading, view signals, positions and performance, and set up a passkey.

An invited user cannot access Telegram source identity, administrative controls or another user's account.

## Enforcement

Protected routes use a reusable permission dependency. A missing or expired session returns `401`. An authenticated account without the required permission receives `403` with:

```json
{
  "detail": {
    "code": "permission_denied",
    "message": "You do not have permission to perform this action.",
    "permission": "security.manage"
  }
}
```

Every authenticated denial is appended to `audit_events` with the actor, role, permission, method, path and request ID. The audit table remains append-only.
