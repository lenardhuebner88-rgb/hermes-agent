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
# The worker-mode cap is 200 while integration remains 800.  The shared
# classifier applies the cap before it assigns a final state, so an oversized
# fallback that leaves no focused test exits 4 as unmapped instead of being
# silently removed after a successful classification.
exec python3 "$SCRIPT_DIR/affected_tests.py" --mode worker "$@"
