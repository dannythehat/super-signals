# Repository Workflow

## Source of truth

GitHub `main` is the source of truth for the application code. Notion records the product blueprint, daily build plan, decisions and acceptance evidence.

## Branches

Use a short-lived branch for each contained change:

- `setup/...` for project setup
- `feature/...` for product work
- `fix/...` for defects
- `security/...` for security changes
- `docs/...` for documentation-only changes

Do not use stale or historical branches as a source for `main`.

## Pull requests

Every meaningful change should explain:

- what changed
- why it changed
- which build-calendar day or issue it belongs to
- how it was tested
- whether secrets, permissions, trading logic or audit behaviour are affected

A pull request must not merge while its required checks are failing.

## Main branch

- `main` is intended to be protected from accidental changes.
- Production deployment will run only from `main`.
- Feature branches must not deploy to production.
- Squash merge is preferred so each pull request produces one clear auditable change on `main`.
- Delete the feature branch after merge when it is no longer needed.

## Evidence

At the end of each build day, record the following in Notion:

- branch and pull request
- merged commit SHA
- tests and checks run
- screenshots or URLs where relevant
- acceptance result
- unresolved blockers

## Secrets

All credentials belong in local environment variables or encrypted platform secrets. Never paste them into source files, commits, issues, pull requests, screenshots or build-calendar evidence.

## Build gates

A calendar day is marked Passed only when its acceptance checks are evidenced. A failed gate is fixed before the project moves forward; the calendar date does not override product safety.
