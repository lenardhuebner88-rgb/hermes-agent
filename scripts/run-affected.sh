#!/usr/bin/env bash
# run-affected.sh [<ref>] — run ONLY the pytest files affected by the diff.
#
# If nothing is affected (the diff maps no .py to an existing test file),
# SKIP pytest entirely (exit 0) — NEVER fall back to the full suite.
#
# This replaces the unsafe one-liner `scripts/run_tests.sh $(scripts/affected-tests.sh)`:
# when affected-tests.sh prints nothing, the command substitution expands to
# nothing and `run_tests.sh` runs with no path args = the WHOLE suite (~31k
# tests, timeout / EXIT 124). That is exactly the "accidentally ran the full
# suite" failure the targeted-scope rule exists to prevent.
#
# The affected-set computation is pure stdlib/bash (works in a bare worktree
# without the venv); on a non-empty diff the forwarded run_tests.sh still needs a
# venv to run pytest and will fail loudly if none is found — which is correct, not
# a silent full-suite run. Forwards an optional <ref> straight to affected-tests.sh
# (e.g. HEAD~1, main...HEAD).
# Frontend gates stay separate as before — this wrapper covers only the pytest part.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FILES=$("$DIR/affected-tests.sh" "$@")
if [ -z "${FILES// /}" ]; then
  echo "run-affected: no affected test files for this diff — skipping pytest (targeted scope; full suite is nightly only)"
  exit 0
fi
exec "$DIR/run_tests.sh" $FILES   # intentionally unquoted: word-split paths into args
