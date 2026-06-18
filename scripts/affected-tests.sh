#!/usr/bin/env bash
# Print the pytest files affected by the current diff — the *targeted* test
# scope (full suite runs only nightly; see AGENTS.md -> Testing). Pure stdlib,
# so it works even in a worktree without the venv.
#
# This only PRINTS the affected files. To RUN them, use scripts/run-affected.sh —
# it skips pytest when nothing is affected instead of letting an empty `$(...)`
# collapse into a bare run_tests.sh = the full suite. Do NOT splice the raw
# `run_tests.sh $(scripts/affected-tests.sh)` yourself.
#   scripts/run-affected.sh         # vs merge-base with main
#   scripts/run-affected.sh HEAD~1  # since a ref
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/affected_tests.py" "$@"
