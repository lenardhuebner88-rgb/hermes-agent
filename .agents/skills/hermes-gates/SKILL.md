---
name: hermes-gates
description: Select and run the correct Hermes backend, frontend, affected, or pre-release verification after code or test changes, and when reviewing claimed green results. Prevents accidental full-suite runs, no-op TypeScript checks, swallowed pipe exit codes, and builds over foreign dirty frontend state.
---

# Hermes Gates

## Select the narrowest meaningful gate

- One backend test or module: `scripts/run_tests.sh <target>`.
- Changed implementation slice: `scripts/run-affected.sh`.
- Frontend changes: `scripts/gate-frontend.sh`; use `--skip-build` when `web_dist` must not be overwritten.
- Before a permitted push/deploy: follow the current project and `/home/piet/vault/00-Canon/conventions-gates.md` scope. Do not invent a full interactive suite when Canon reserves it for nightly runs.

## Preserve gate integrity

1. Check `git status --short --branch` immediately before running a gate. Attribute foreign dirty files and stop if they contaminate the selected scope.
2. Use the repository wrappers so HOME, HERMES_HOME, credentials, locale, timezone, and subprocess state stay isolated.
3. Never treat bare `tsc --noEmit` or `npm run typecheck` as proof here; the solution config requires `tsc -b` through the frontend gate.
4. Never hand-roll a pipe that can return the consumer's exit code instead of the test command's. Preserve `pipefail` or avoid the pipe.
5. Do not let tests write to a real `~/.hermes/`.
6. Report the exact command, exit code, affected scope, skipped stages, and whether generated served assets changed.

Re-run the same affected scope independently when acting as verifier; do not broaden it merely to look more thorough.
