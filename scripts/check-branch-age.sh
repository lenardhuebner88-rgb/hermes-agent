#!/usr/bin/env bash
# check-branch-age.sh — drift signal when HEAD falls behind local main.
#
# Usage: scripts/check-branch-age.sh
#   (usually invoked by run-affected.sh / gate-frontend.sh at gate start)
#
# Compares HEAD against LOCAL main only — no network fetch. Intentional:
# the coordination checker covers file claims; this covers "main moved under
# a long-lived worktree/session" without requiring online remotes.
#
# Exit / messaging:
#   BEHIND == 0          → silent, exit 0
#   BEHIND 1..5          → one stderr line, exit 0
#   BEHIND >5            → stderr red line, exit 1
#   BEHIND >5 + override → stderr line with (override aktiv), exit 0
#
# Env:
#   HERMES_GATE_STALE_OK=1  — conscious override for legitimate chain worktrees
#                             when >5 commits behind local main.
#
# If `main` is not resolvable (no such ref / not a git repo), exits 0 silently.
set -euo pipefail

BEHIND="$(git rev-list --count HEAD..main 2>/dev/null || echo 0)"

# Non-numeric / empty fallback (should not happen, but keep the gate quiet).
case "${BEHIND}" in
  ''|*[!0-9]*) BEHIND=0 ;;
esac

if [ "${BEHIND}" -eq 0 ]; then
  exit 0
fi

if [ "${BEHIND}" -le 5 ]; then
  echo "[branch-age] HEAD ist ${BEHIND} Commits hinter main — rebase empfohlen." >&2
  exit 0
fi

if [ "${HERMES_GATE_STALE_OK:-}" = "1" ]; then
  echo "[branch-age] HEAD ist ${BEHIND} Commits hinter main (>5) — ROT. Override: HERMES_GATE_STALE_OK=1 (override aktiv)" >&2
  exit 0
fi

echo "[branch-age] HEAD ist ${BEHIND} Commits hinter main (>5) — ROT. Override: HERMES_GATE_STALE_OK=1" >&2
exit 1
